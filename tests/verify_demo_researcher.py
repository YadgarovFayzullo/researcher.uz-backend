"""Демо-профиль исследователя: изоляция от витрины и автовход по ссылке.

Что стережём:

  * демо-профиль не попадает в карусель на главной и отдаётся с is_demo;
  * «Прикрепить публикацию» принимает только демо-статьи, импорт по DOI,
    подсказки карточек, заявка «Это я» и привязка ORCID для демо закрыты;
  * демо-копия с тем же заголовком и файлом не считается дублем настоящей
    статьи (а настоящий дубль по-прежнему ловится);
  * GET /auth/demo-login пускает по верному токену и не пускает по неверному
    или просроченному.

Нужна живая БД с миграцией profiles.metadata.
PYTHONPATH=. .venv/bin/python tests/verify_demo_researcher.py
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from httpx import ASGITransport
from sqlalchemy import delete, select

from src.domain.authors import AuthorCardDomain, AuthorCardError
from src.domain.demo import DEMO_LOGIN, demo_login_hash
from src.domain.issue_checks import check_platform_duplicates
from src.domain.orcid import OrcidDomain, OrcidTaken
from src.domain.researcher import CabinetError, ResearcherDomain
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    Author,
    AuthorClaim,
    Issue,
    Journal,
    Profile,
    User,
)
from src.main import app

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results: list[bool] = []

TAG = "DEMORES"
TITLE = f"{TAG} одинаковый заголовок статьи для проверки платформенных дублей"
PDF = "https://example.com/demores-same-file.pdf"
TOKEN = uuid.uuid4().hex + uuid.uuid4().hex


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  [{PASS if ok else FAIL}] {name}: got={got!r} want={want!r}")


async def raises(db, name, exc, coro):
    try:
        await coro
        check(name, "без исключения", exc.__name__)
    except exc:
        check(name, exc.__name__, exc.__name__)
    await db.rollback()


async def _sweep(db, user_ids):
    art_ids = (
        await db.execute(select(Article.id).where(Article.title.like(f"{TAG}%")))
    ).scalars().all()
    for stmt in (
        delete(ArticleAuthor).where(ArticleAuthor.article_id.in_(art_ids)),
        delete(AuthorClaim).where(AuthorClaim.profile_id.in_(user_ids)),
        delete(Author).where(Author.slug.like("demores-%")),
        delete(Article).where(Article.id.in_(art_ids)),
        delete(Issue).where(Issue.title.like(f"{TAG}%")),
        delete(Journal).where(Journal.name.like(f"{TAG}%")),
        delete(Profile).where(Profile.id.in_(user_ids)),
        delete(User).where(User.id.in_(user_ids)),
    ):
        await db.execute(stmt)
    await db.commit()


async def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    demo_id, normal_id = uuid.uuid4(), uuid.uuid4()
    user_ids = [demo_id, normal_id]
    researcher = ResearcherDomain()

    async with AsyncSessionLocal() as db:
        await _sweep(db, user_ids)

        db.add_all([User(id=u, email=f"demores-{u.hex[:8]}@example.com") for u in user_ids])
        await db.flush()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        db.add_all([
            Profile(
                id=demo_id, full_name=f"{TAG} Demo Researcher", role="authenticated",
                avatar_url="https://example.com/demo.png",
                meta={"demo": True, DEMO_LOGIN: {"sha256": demo_login_hash(TOKEN), "expires_at": future}},
            ),
            Profile(
                id=normal_id, full_name=f"{TAG} Normal Researcher", role="authenticated",
                avatar_url="https://example.com/normal.png",
            ),
        ])
        demo_journal = Journal(name=f"{TAG} demo journal", slug=f"demores-demo-{suffix}",
                               type="journal", meta={"demo": True})
        real_journal = Journal(name=f"{TAG} real journal", slug=f"demores-real-{suffix}",
                               type="journal", meta={})
        db.add_all([demo_journal, real_journal])
        await db.flush()
        demo_issue = Issue(journal_id=demo_journal.id, title=f"{TAG} demo issue", year=2026)
        real_issue = Issue(journal_id=real_journal.id, title=f"{TAG} real issue", year=2026)
        db.add_all([demo_issue, real_issue])
        await db.flush()
        demo_art = Article(issue_id=demo_issue.id, title=TITLE, pdf=PDF, published=True,
                           slug=f"demores-demo-a-{suffix}")
        real_art = Article(issue_id=real_issue.id, title=TITLE, pdf=PDF, published=True,
                           slug=f"demores-real-a-{suffix}", doi=f"10.5555/demores.{suffix}")
        db.add_all([demo_art, real_art])
        card = Author(name_key=f"demores|x.{suffix}", slug=f"demores-card-{suffix}",
                      display_name=f"{TAG} Card", works_count=1)
        db.add(card)
        await db.flush()
        # По публикации каждому: в карусель профиль пускают только с ними, и
        # без этих строк «демо не в карусели» проходило бы само собой.
        db.add_all([
            ArticleAuthor(article_id=demo_art.id, profile_id=demo_id, author_order=0,
                          author_name=f"{TAG} Demo Researcher"),
            ArticleAuthor(article_id=real_art.id, profile_id=normal_id, author_order=0,
                          author_name=f"{TAG} Normal Researcher"),
        ])
        await db.commit()
        # Значения запоминаем сразу: rollback в raises() сбрасывает объекты, и
        # обращение к их атрибутам в async-сессии падает с MissingGreenlet.
        demo_art_id, real_art_id = demo_art.id, real_art.id
        real_issue_id, card_slug, real_doi = real_issue.id, card.slug, real_art.doi

        print("\n--- витрина ---")
        shown = {p["id"] for p in await researcher.public_profiles(db, 50)}
        check("демо не в карусели", str(demo_id) in shown, False)
        check("обычный профиль в карусели", str(normal_id) in shown, True)
        # Аватар ставит вход через Google всем подряд — витрина не должна
        # набиваться пустыми карточками «0 публикаций».
        await db.execute(delete(ArticleAuthor).where(ArticleAuthor.profile_id == normal_id))
        await db.commit()
        empty = {p["id"] for p in await researcher.public_profiles(db, 50)}
        check("профиль без публикаций не в карусели", str(normal_id) in empty, False)
        check("is_demo у демо", (await researcher.get_profile_by_user_id(db, str(demo_id)))["is_demo"], True)
        check("is_demo у обычного", (await researcher.get_profile_by_user_id(db, str(normal_id)))["is_demo"], False)

        print("\n--- кнопки кабинета ---")
        await raises(db, "настоящую статью не прикрепить", CabinetError,
                     researcher.claim_article(db, user_id=demo_id, article_id=real_art_id))
        await researcher.claim_article(db, user_id=demo_id, article_id=demo_art_id)
        linked = (await db.execute(select(ArticleAuthor.id).where(
            ArticleAuthor.article_id == demo_art_id, ArticleAuthor.profile_id == demo_id))).first()
        check("демо-статья прикрепляется", linked is not None, True)
        await raises(db, "импорт по DOI закрыт", CabinetError,
                     researcher.claim_articles_by_dois(db, user_id=demo_id, dois=[real_doi]))
        check("подсказок карточек нет", await researcher.suggest_author_cards(db, user_id=demo_id), [])
        await raises(db, "«Это я» закрыто", AuthorCardError,
                     AuthorCardDomain().request_claim(db, card_slug, str(demo_id)))
        await raises(db, "ORCID не привязывается", OrcidTaken,
                     OrcidDomain().link_orcid(db, user_id=demo_id, orcid="0000-0002-1825-0097"))

        print("\n--- проверка дублей ---")
        real_obj = (await db.execute(select(Article).where(Article.id == real_art_id))).scalars().first()
        found = await check_platform_duplicates(db, [real_obj])
        fields = sorted(p.field for p in found.get(real_art_id, []))
        check("демо-копия не дубль (ни заголовок, ни файл)", fields, [])
        twin = Article(issue_id=real_issue_id, title=TITLE, published=True,
                       slug=f"demores-real-b-{suffix}")
        db.add(twin)
        await db.commit()
        found = await check_platform_duplicates(db, [real_obj])
        check("настоящий дубль ловится", any(p.field == "title" for p in found.get(real_art_id, [])), True)

        print("\n--- автовход и API ---")
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http:
            r = await http.get("/auth/demo-login", params={"t": TOKEN})
            check("верный токен → редирект", r.status_code, 303)
            check("ведёт в демо-профиль", r.headers.get("location", "").endswith(f"/ru/researcher/u/{demo_id}"), True)
            check("выдаёт сессию", "set-cookie" in r.headers, True)

            r = await http.get("/auth/demo-login", params={"t": "x" * 40})
            check("чужой токен → на вход", "demo_error" in r.headers.get("location", ""), True)
            check("без сессии", "set-cookie" in r.headers, False)

            page = (await http.get(f"/researcher/u/{demo_id}")).json()
            check("API профиля отдаёт is_demo", page["profile"].get("is_demo"), True)
            listed = {p["id"] for p in (await http.get("/researcher/public-profiles", params={"limit": 50})).json()}
            check("API карусели без демо", str(demo_id) in listed, False)

            prof = (await db.execute(select(Profile).where(Profile.id == demo_id))).scalars().first()
            past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            prof.meta = {**prof.meta, DEMO_LOGIN: {"sha256": demo_login_hash(TOKEN), "expires_at": past}}
            await db.commit()
            r = await http.get("/auth/demo-login", params={"t": TOKEN})
            check("просроченный токен → на вход", "demo_error" in r.headers.get("location", ""), True)

        print("\n--- уборка ---")
        await _sweep(db, user_ids)
        left = (await db.execute(select(Profile).where(Profile.id.in_(user_ids)))).scalars().all()
        check("фикстур не осталось", len(left), 0)

    total_n, ok_n = len(results), sum(results)
    print(f"\n{'='*46}\nИтог: {ok_n}/{total_n} " + ("— всё зелёное" if ok_n == total_n else "— ЕСТЬ ПАДЕНИЯ"))
    return 0 if ok_n == total_n else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

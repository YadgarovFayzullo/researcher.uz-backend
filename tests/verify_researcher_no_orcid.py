"""Профиль исследователя без ORCID — страница по id аккаунта.

Регистрация через Google профиль заводит, но адресовать его было нечем:
публичная карточка ходила только по ORCID, а claim статьи падал с
«no ORCID linked». Что стережём:

  * GET /researcher/u/{id} отдаёт карточку профиля без ORCID;
  * прячет `is_public = false` и отвечает 404 на мусорный id (не 500);
  * профиль с ORCID по этому адресу отдаётся так же, вместе с внешними работами;
  * claim работает без ORCID, и статья попадает в /researcher/u/{id}/publications;
  * повторный claim идемпотентен, а чужая строка автора без ORCID своей не
    считается (условие «уже привязано» не должно ловить orcid IS NULL);
  * старый ORCID-маршрут не изменился.

Нужна живая БД. PYTHONPATH=. .venv/bin/python tests/verify_researcher_no_orcid.py
"""
from __future__ import annotations

import asyncio
import uuid

import httpx
from httpx import ASGITransport
from sqlalchemy import delete, select

from src.domain.researcher import ResearcherDomain
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    Issue,
    Journal,
    Profile,
    ResearcherWork,
    User,
)
from src.main import app

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results: list[bool] = []

TAG = "NOORCID"


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  [{PASS if ok else FAIL}] {name}: got={got!r} want={want!r}")


async def _sweep(db, user_ids, orcids):
    art_ids = (
        await db.execute(select(Article.id).where(Article.title.like(f"{TAG} %")))
    ).scalars().all()
    for stmt in (
        delete(ArticleAuthor).where(ArticleAuthor.article_id.in_(art_ids)),
        delete(Article).where(Article.id.in_(art_ids)),
        delete(Issue).where(Issue.title.like(f"{TAG} %")),
        delete(Journal).where(Journal.name.like(f"{TAG} %")),
        delete(ResearcherWork).where(ResearcherWork.orcid.in_(orcids)),
        delete(Profile).where(Profile.id.in_(user_ids)),
        delete(User).where(User.id.in_(user_ids)),
    ):
        await db.execute(stmt)
    await db.commit()


async def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    google_id = uuid.uuid4()
    orcid_id = uuid.uuid4()
    hidden_id = uuid.uuid4()
    stranger_id = uuid.uuid4()
    orcid = "0000-0002-1825-0097"
    user_ids = [google_id, orcid_id, hidden_id, stranger_id]

    domain = ResearcherDomain()

    async with AsyncSessionLocal() as db:
        await _sweep(db, user_ids, [orcid])

        # ------------------------------------------------------- фикстуры
        db.add_all([User(id=uid, email=f"{TAG.lower()}-{uid.hex[:6]}@example.com")
                    for uid in user_ids])
        await db.flush()
        db.add_all([
            Profile(id=google_id, full_name=f"{TAG} Google", role="authenticated",
                    avatar_url="https://example/a.png", workplace="Institute"),
            Profile(id=orcid_id, full_name=f"{TAG} Orcid", role="authenticated",
                    orcid_id=orcid),
            Profile(id=hidden_id, full_name=f"{TAG} Hidden", role="authenticated",
                    is_public=False),
            Profile(id=stranger_id, full_name=f"{TAG} Stranger", role="authenticated"),
        ])
        db.add(ResearcherWork(orcid=orcid, put_code=f"{TAG}-1", title=f"{TAG} work"))

        journal = Journal(name=f"{TAG} journal {suffix}", slug=f"noorcid-{suffix}")
        db.add(journal)
        await db.flush()
        issue = Issue(journal_id=journal.id, title=f"{TAG} issue", year=2026)
        db.add(issue)
        await db.flush()
        article = Article(
            issue_id=issue.id,
            title=f"{TAG} article",
            slug=f"noorcid-a-{suffix}",
            published=True,
        )
        other = Article(
            issue_id=issue.id,
            title=f"{TAG} article two",
            slug=f"noorcid-b-{suffix}",
            published=True,
        )
        db.add_all([article, other])
        await db.flush()
        # Чужая строка автора без ORCID на второй статье — ловушка для условия
        # «уже привязано»: сравнение orcid IS NULL приняло бы её за свою.
        db.add(
            ArticleAuthor(
                article_id=other.id,
                author_order=0,
                author_name=f"{TAG} Stranger",
                profile_id=stranger_id,
            )
        )
        await db.commit()

        # ----------------------------------------------------- claim без iD
        print("\n--- claim без ORCID ---")
        await domain.claim_article(db, user_id=google_id, article_id=article.id)
        mine = (
            await db.execute(
                select(ArticleAuthor).where(ArticleAuthor.profile_id == google_id)
            )
        ).scalars().all()
        check("статья привязалась к профилю", len(mine), 1)
        check("ORCID в строке автора пуст", mine[0].orcid, None)

        await domain.claim_article(db, user_id=google_id, article_id=article.id)
        mine = (
            await db.execute(
                select(ArticleAuthor).where(ArticleAuthor.profile_id == google_id)
            )
        ).scalars().all()
        check("повторный claim не задвоил", len(mine), 1)

        await domain.claim_article(db, user_id=google_id, article_id=other.id)
        mine_ids = {
            r.article_id
            for r in (
                await db.execute(
                    select(ArticleAuthor).where(ArticleAuthor.profile_id == google_id)
                )
            ).scalars().all()
        }
        check("чужая строка без iD не помешала", other.id in mine_ids, True)

        # --------------------------------------------------------- страницы
        print("\n--- страница по id аккаунта ---")
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver", timeout=60
        ) as http:
            r = await http.get(f"/researcher/u/{google_id}")
            check("карточка без ORCID отдаётся", r.status_code, 200)
            body = r.json()
            check("имя на месте", body["profile"]["full_name"], f"{TAG} Google")
            check("ORCID пуст", body["profile"]["orcid"], None)
            check("внешних работ нет", body["works"], [])

            pubs = await http.get(f"/researcher/u/{google_id}/publications")
            check("публикации отдаются", len(pubs.json()), 2)

            r = await http.get(f"/researcher/u/{orcid_id}")
            check("профиль с ORCID по тому же адресу", r.status_code, 200)
            check("ORCID отдан", r.json()["profile"]["orcid"], orcid)
            check("внешние работы приехали", len(r.json()["works"]), 1)

            check(
                "непубличный профиль скрыт",
                (await http.get(f"/researcher/u/{hidden_id}")).status_code,
                404,
            )
            check(
                "мусорный id — 404, а не 500",
                (await http.get("/researcher/u/not-a-uuid")).status_code,
                404,
            )
            check(
                "несуществующий id — 404",
                (await http.get(f"/researcher/u/{uuid.uuid4()}")).status_code,
                404,
            )

            print("\n--- старый маршрут ---")
            r = await http.get(f"/researcher/{orcid}")
            check("ORCID-страница жива", r.status_code, 200)
            check("её ORCID на месте", r.json()["profile"]["orcid"], orcid)

        # ---------------------------------------------------------- уборка
        print("\n--- уборка ---")
        await _sweep(db, user_ids, [orcid])
        left = (
            await db.execute(select(Profile).where(Profile.id.in_(user_ids)))
        ).scalars().all()
        check("фикстур не осталось", len(left), 0)

    total_n, ok_n = len(results), sum(results)
    print(f"\n{'='*46}\nИтог: {ok_n}/{total_n} " + ("— всё зелёное" if ok_n == total_n else "— ЕСТЬ ПАДЕНИЯ"))
    return 0 if ok_n == total_n else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

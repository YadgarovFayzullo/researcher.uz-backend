"""«Это я» привязывает подпись под статьёй, а не дописывает вторую.

Повод: у владельца платформы на профиле висели двое — «F.N. Yadgarov» (подпись
из статьи) и «Fayzullo Yadgarov» (строка, которую добавил кабинет). Один
человек стоял под статьёй дважды, попадал к себе же в соавторы, а
`backfill_authors.py` заводил ему из второго написания вторую карточку автора.

Стережём:
  * claim ставит profile_id на СУЩЕСТВУЮЩУЮ подпись, если похожая ровно одна,
    и новой строки не появляется;
  * двух однофамильцев в одной статье claim не разбирает — заводит свою строку
    с меткой from_claim (её разберёт владелец заявкой на карточку);
  * однофамилец с другим инициалом («Yadgarov N.») чужую подпись не забирает;
  * статьи, где человека в метаданных нет, по-прежнему прикрепляются;
  * unclaim удаляет строку кабинета, но настоящую подпись только отвязывает —
    иначе автор стирался бы из метаданных статьи одним нажатием;
  * профиль после привязки не показывает человека в его же соавторах.

Запуск: PYTHONPATH=. .venv/bin/python tests/verify_claim_signatures.py
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete, func, select

from src.domain.author_names import may_be_same_person
from src.domain.content import AuthorDomain
from src.domain.researcher import ResearcherDomain
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    Issue,
    Journal,
    Profile,
    User,
)

GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"
results: list[bool] = []

TAG = "CLAIMT"
ORCID = "0009-0007-4562-9999"


def check(name: str, got, want):
    ok = got == want
    results.append(ok)
    tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{tag}] {name}: got={got!r} want={want!r}")


def check_names() -> None:
    print("\n--- мягкое сличение имён (только внутри статьи) ---")
    check("инициалы — подмножество", may_be_same_person("Fayzullo Yadgarov", "F.N. Yadgarov"), True)
    check("порядок слов не важен", may_be_same_person("Fayzullo Yadgarov", "Yadgarov F."), True)
    check("кириллица и латиница", may_be_same_person("Файзулло Ядгаров", "F.N. Yadgarov"), True)
    check("полное ФИО и инициалы", may_be_same_person("Yadgarov Fayzullo Nodirovich", "F.N. Yadgarov"), True)
    # Ради этого требования и нужна проверка подмножества: иначе claim забрал бы
    # подпись однофамильца.
    check("другой инициал — не он", may_be_same_person("Fayzullo Yadgarov", "N. Yadgarov"), False)
    check("другая фамилия", may_be_same_person("Fayzullo Yadgarov", "F. Karimov"), False)
    check("одно слово ничего не значит", may_be_same_person("Yadgarov", "F.N. Yadgarov"), False)


async def _sweep(db) -> None:
    journal_ids = (
        await db.execute(select(Journal.id).where(Journal.name.like(f"{TAG} %")))
    ).scalars().all()
    issue_ids = (
        (await db.execute(select(Issue.id).where(Issue.journal_id.in_(journal_ids)))).scalars().all()
        if journal_ids
        else []
    )
    article_ids = (
        (await db.execute(select(Article.id).where(Article.issue_id.in_(issue_ids)))).scalars().all()
        if issue_ids
        else []
    )
    user_ids = (
        await db.execute(select(User.id).where(User.email.like(f"{TAG.lower()}-%")))
    ).scalars().all()
    for stmt in (
        delete(ArticleAuthor).where(ArticleAuthor.article_id.in_(article_ids)),
        delete(Article).where(Article.id.in_(article_ids)),
        delete(Issue).where(Issue.id.in_(issue_ids)),
        delete(Journal).where(Journal.id.in_(journal_ids)),
        delete(Profile).where(Profile.id.in_(user_ids)),
        delete(User).where(User.id.in_(user_ids)),
    ):
        await db.execute(stmt)
    await db.commit()


async def _rows(db, article_id: int) -> list[ArticleAuthor]:
    return list(
        (
            await db.execute(
                select(ArticleAuthor)
                .where(ArticleAuthor.article_id == article_id)
                .order_by(ArticleAuthor.author_order)
            )
        )
        .scalars()
        .all()
    )


async def main() -> int:
    check_names()

    suffix = uuid.uuid4().hex[:8]
    domain = ResearcherDomain()
    async with AsyncSessionLocal() as db:
        await _sweep(db)

        user = User(email=f"{TAG.lower()}-{suffix}@example.test")
        db.add(user)
        await db.flush()
        db.add(Profile(id=user.id, full_name="Fayzullo Yadgarov", orcid_id=ORCID))
        journal = Journal(name=f"{TAG} journal {suffix}", slug=f"claimt-{suffix}")
        db.add(journal)
        await db.flush()
        issue = Issue(journal_id=journal.id, title=f"{TAG} issue", year=2025)
        db.add(issue)
        await db.flush()

        # 1. Обычный случай: человек подписан инициалами.
        one = Article(
            title=f"{TAG} одна подпись",
            slug=f"claimt-one-{suffix}",
            issue_id=issue.id,
            authors='["F.N. Yadgarov", "A. Karimov"]',
        )
        # 2. Два однофамильца: подпись не угадать.
        twins = Article(
            title=f"{TAG} два однофамильца",
            slug=f"claimt-twins-{suffix}",
            issue_id=issue.id,
            authors='["F.N. Yadgarov", "F. Yadgarov"]',
        )
        # 3. Однофамилец с другим инициалом: чужую подпись брать нельзя.
        other = Article(
            title=f"{TAG} однофамилец",
            slug=f"claimt-other-{suffix}",
            issue_id=issue.id,
            authors='["N. Yadgarov"]',
        )
        # 4. Человека в метаданных забыли.
        absent = Article(
            title=f"{TAG} без подписи",
            slug=f"claimt-absent-{suffix}",
            issue_id=issue.id,
            authors='["K. Allanazarov"]',
        )
        db.add_all([one, twins, other, absent])
        await db.flush()
        for article, names in (
            (one, ["F.N. Yadgarov", "A. Karimov"]),
            (twins, ["F.N. Yadgarov", "F. Yadgarov"]),
            (other, ["N. Yadgarov"]),
            (absent, ["K. Allanazarov"]),
        ):
            db.add_all(
                ArticleAuthor(article_id=article.id, author_order=i, author_name=n)
                for i, n in enumerate(names)
            )
        await db.commit()
        user_id = user.id

        print("\n--- claim привязывает существующую подпись ---")
        before = len(await _rows(db, one.id))
        await domain.claim_article(db, user_id=user_id, article_id=one.id)
        rows = await _rows(db, one.id)
        check("новой строки не появилось", len(rows), before)
        mine = [r for r in rows if r.profile_id == user_id]
        check("привязана ровно одна", len(mine), 1)
        check("это подпись из статьи", mine[0].author_name, "F.N. Yadgarov")
        check("iD проставлен", mine[0].orcid, ORCID)
        check("и это не строка кабинета", mine[0].from_claim, False)

        print("\n--- повторный claim ничего не ломает ---")
        await domain.claim_article(db, user_id=user_id, article_id=one.id)
        check("строк столько же", len(await _rows(db, one.id)), before)

        print("\n--- два однофамильца: не угадываем ---")
        await domain.claim_article(db, user_id=user_id, article_id=twins.id)
        rows = await _rows(db, twins.id)
        check("добавлена своя строка", len(rows), 3)
        mine = [r for r in rows if r.profile_id == user_id]
        check("она помечена как строка кабинета", [r.from_claim for r in mine], [True])
        check("чужие подписи не тронуты",
              [r.profile_id for r in rows if not r.from_claim], [None, None])

        print("\n--- однофамилец с другим инициалом ---")
        await domain.claim_article(db, user_id=user_id, article_id=other.id)
        rows = await _rows(db, other.id)
        check("подпись N. Yadgarov не забрали",
              [r.profile_id for r in rows if r.author_name == "N. Yadgarov"], [None])
        check("заведена отдельная строка", len(rows), 2)

        print("\n--- статья без подписи прикрепляется как раньше ---")
        await domain.claim_article(db, user_id=user_id, article_id=absent.id)
        rows = await _rows(db, absent.id)
        check("строка добавлена", len(rows), 2)
        check("и помечена", [r.from_claim for r in rows if r.profile_id == user_id], [True])

        print("\n--- профиль не показывает человека в своих соавторах ---")
        pubs = await AuthorDomain().list_publications_by_orcid(db, ORCID)
        published = {p["article"]["title"]: p for p in pubs}
        card = published.get(f"{TAG} одна подпись")
        check("статья на профиле есть", card is not None, True)
        if card:
            chips = [c for c in card["article"]["coauthors"] if not c.get("self")]
            check("в соавторах только настоящий соавтор",
                  [c["name"] for c in chips], ["A. Karimov"])

        print("\n--- unclaim: строку кабинета удаляем, подпись отвязываем ---")
        await domain.unclaim_article(db, user_id=user_id, article_id=one.id)
        rows = await _rows(db, one.id)
        check("подпись осталась в статье", len(rows), before)
        signature = [r for r in rows if r.author_name == "F.N. Yadgarov"][0]
        check("но уже ничья", signature.profile_id, None)
        check("и без чужого iD", signature.orcid, None)

        await domain.unclaim_article(db, user_id=user_id, article_id=absent.id)
        rows = await _rows(db, absent.id)
        check("строка кабинета удалена", len(rows), 1)
        check("автор статьи не пострадал", rows[0].author_name, "K. Allanazarov")

        left = await db.scalar(
            select(func.count(ArticleAuthor.id)).where(
                ArticleAuthor.article_id == absent.id,
                ArticleAuthor.profile_id == user_id,
            )
        )
        check("привязок не осталось", left, 0)

        await _sweep(db)

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

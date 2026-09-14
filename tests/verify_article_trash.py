"""Удалённая статья лежит в корзине и возвращается целиком.

Стережём:
  * delete_articles кладёт снимок в article_trash в той же транзакции;
  * restore возвращает статью с прежним id, слагом, счётчиками, подписями
    авторов, просмотрами (article_interactions), текстом и отпечатками, а
    обнулённую удалением ссылку цитирования — обратно;
  * занятый слаг и удалённый выпуск — отказ TrashConflict, статья не
    появляется;
  * статья из погашенного выпуска возвращается неопубликованной;
  * purge_expired удаляет только записи старше срока.

Запуск: PYTHONPATH=. .venv/bin/python tests/verify_article_trash.py
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete, func, select, text, update

from src.domain import article_trash
from src.domain.article import ArticleDomain
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    ArticleFingerprint,
    ArticleInteraction,
    ArticleReference,
    ArticleText,
    ArticleTrash,
    Issue,
    Journal,
)

GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"
results: list[bool] = []

TAG = "TRASHT"

BODY = " ".join(
    f"слово{i % 97} исследование методика результат анализ" for i in range(400)
)


def check(name: str, got, want):
    ok = got == want
    results.append(ok)
    tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{tag}] {name}: got={got!r} want={want!r}")


async def _count(db, model, *where):
    return await db.scalar(select(func.count()).select_from(model).where(*where))


async def _sweep(db):
    ids = (
        await db.execute(select(Article.id).where(Article.title.like(f"{TAG} %")))
    ).scalars().all()
    for stmt in (
        delete(ArticleReference).where(ArticleReference.raw.like(f"{TAG} %")),
        delete(ArticleInteraction).where(ArticleInteraction.article_id.in_(ids)),
        delete(ArticleAuthor).where(ArticleAuthor.article_id.in_(ids)),
        delete(Article).where(Article.id.in_(ids)),
        delete(ArticleTrash).where(ArticleTrash.title.like(f"{TAG} %")),
        delete(Issue).where(Issue.title.like(f"{TAG} %")),
        delete(Journal).where(Journal.name.like(f"{TAG} %")),
    ):
        await db.execute(stmt)
    await db.commit()


async def _trash_id(db, article_id: int) -> int | None:
    return await db.scalar(
        select(ArticleTrash.id)
        .where(ArticleTrash.article_id == article_id)
        .order_by(ArticleTrash.id.desc())
    )


async def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    articles = ArticleDomain()
    async with AsyncSessionLocal() as db:
        await _sweep(db)

        journal = Journal(name=f"{TAG} journal {suffix}", slug=f"trasht-{suffix}")
        db.add(journal)
        await db.flush()
        issue = Issue(journal_id=journal.id, title=f"{TAG} issue", year=2026)
        gone = Issue(journal_id=journal.id, title=f"{TAG} issue gone", year=2026)
        db.add_all([issue, gone])
        await db.flush()
        a1 = Article(
            title=f"{TAG} удаляемая",
            slug=f"trasht-a1-{suffix}",
            issue_id=issue.id,
            published=True,
            pages="10-20",
            views_count=123,
            downloads_count=7,
            meta={"note": "keep"},
        )
        a2 = Article(
            title=f"{TAG} цитирующая", slug=f"trasht-a2-{suffix}", issue_id=issue.id
        )
        a3 = Article(
            title=f"{TAG} в удалённом выпуске", slug=f"trasht-a3-{suffix}", issue_id=gone.id
        )
        db.add_all([a1, a2, a3])
        await db.flush()
        db.add_all(
            [
                ArticleAuthor(article_id=a1.id, author_name=f"{TAG} Автор", author_order=1),
                ArticleInteraction(article_id=a1.id, view=1),
                ArticleInteraction(article_id=a1.id, download=1),
                ArticleReference(
                    article_id=a2.id, cited_article_id=a1.id, raw=f"{TAG} ссылка", position=1
                ),
            ]
        )
        await db.commit()
        a1_id, a2_id, a3_id, gone_id = a1.id, a2.id, a3.id, gone.id
        issue_id, journal_name = issue.id, journal.name
        from src.domain.plagiarism import PlagiarismDomain

        await PlagiarismDomain().index_article(db, a1_id, BODY)
        prints_before = await _count(db, ArticleFingerprint, ArticleFingerprint.article_id == a1_id)

        print("\n--- удаление кладёт снимок в корзину ---")
        check("удалена одна", await articles.delete_articles(db, [a1_id]), 1)
        check("статьи нет", await _count(db, Article, Article.id == a1_id), 0)
        tid = await _trash_id(db, a1_id)
        check("запись в корзине есть", tid is not None, True)
        ref = await db.scalar(
            select(ArticleReference.cited_article_id).where(ArticleReference.article_id == a2_id)
        )
        check("ссылка цитирования обнулена", ref, None)
        listing = await article_trash.list_trash(db, q=f"{TAG} удаляемая")
        item = listing["items"][0] if listing["items"] else {}
        check("видна в списке", listing["total"], 1)
        check("в списке журнал", item.get("journal_name"), journal_name)
        check("в списке просмотры", item.get("views"), 123)
        check("выпуск существует", item.get("issue_exists"), True)

        print("\n--- восстановление ---")
        res = await article_trash.restore(db, tid)
        check("вернулся прежний id", res["article_id"], a1_id)
        check("журнал для сброса кэша", res["journal_slug"], f"trasht-{suffix}")
        check("предупреждений нет", res["warnings"], [])
        back = await db.get(Article, a1_id)
        await db.refresh(back)
        check("слаг", back.slug, f"trasht-a1-{suffix}")
        check("выпуск", back.issue_id, issue_id)
        check("опубликована", back.published, True)
        check("просмотры", back.views_count, 123)
        check("скачивания", back.downloads_count, 7)
        check("metadata", back.meta, {"note": "keep"})
        check("поисковый вектор пересчитан", back.search_vector is not None, True)
        check("подписи авторов", await _count(db, ArticleAuthor, ArticleAuthor.article_id == a1_id), 1)
        check(
            "записи просмотров",
            await _count(db, ArticleInteraction, ArticleInteraction.article_id == a1_id),
            2,
        )
        check("текст", await _count(db, ArticleText, ArticleText.article_id == a1_id), 1)
        check(
            "отпечатки пересчитаны",
            await _count(db, ArticleFingerprint, ArticleFingerprint.article_id == a1_id),
            prints_before,
        )
        ref = await db.scalar(
            select(ArticleReference.cited_article_id).where(ArticleReference.article_id == a2_id)
        )
        check("ссылка цитирования вернулась", ref, a1_id)
        check("корзина пуста", await _count(db, ArticleTrash, ArticleTrash.id == tid), 0)
        try:
            await article_trash.restore(db, tid)
            check("повтор — TrashNotFound", "восстановлена", "TrashNotFound")
        except article_trash.TrashNotFound:
            check("повтор — TrashNotFound", "TrashNotFound", "TrashNotFound")

        print("\n--- занятый слаг ---")
        await articles.delete_articles(db, [a1_id])
        tid = await _trash_id(db, a1_id)
        dup = Article(title=f"{TAG} перезалитая", slug=f"trasht-a1-{suffix}", issue_id=issue_id)
        db.add(dup)
        await db.commit()
        dup_id = dup.id
        try:
            await article_trash.restore(db, tid)
            check("отказ TrashConflict", "восстановлена", "TrashConflict")
        except article_trash.TrashConflict:
            await db.rollback()
            check("отказ TrashConflict", "TrashConflict", "TrashConflict")
        check("статья не появилась", await _count(db, Article, Article.id == a1_id), 0)
        check("запись осталась в корзине", await _count(db, ArticleTrash, ArticleTrash.id == tid), 1)
        await articles.delete_articles(db, [dup_id])

        print("\n--- погашенный выпуск ---")
        await db.execute(
            update(Issue).where(Issue.id == issue_id).values(meta={"blocked": {"reason": TAG}})
        )
        await db.commit()
        res = await article_trash.restore(db, tid)
        back = await db.get(Article, a1_id)
        await db.refresh(back)
        check("восстановлена неопубликованной", back.published, False)
        check("с предупреждением", len(res["warnings"]), 1)

        print("\n--- удалённый выпуск ---")
        await articles.delete_articles(db, [a3_id])
        tid3 = await _trash_id(db, a3_id)
        await db.execute(delete(Issue).where(Issue.id == gone_id))
        await db.commit()
        listing = await article_trash.list_trash(db, q=f"{TAG} в удалённом")
        check("список: выпуска нет", listing["items"][0]["issue_exists"], False)
        try:
            await article_trash.restore(db, tid3)
            check("отказ TrashConflict", "восстановлена", "TrashConflict")
        except article_trash.TrashConflict:
            await db.rollback()
            check("отказ TrashConflict", "TrashConflict", "TrashConflict")

        print("\n--- чистка по сроку ---")
        await db.execute(
            update(ArticleTrash)
            .where(ArticleTrash.id == tid3)
            .values(deleted_at=text("now() - interval '31 days'"))
        )
        await articles.delete_articles(db, [a2_id])
        fresh = await _trash_id(db, a2_id)
        await db.commit()
        purged = await article_trash.purge_expired(db)
        check("просроченная удалена", await _count(db, ArticleTrash, ArticleTrash.id == tid3), 0)
        check("свежая осталась", await _count(db, ArticleTrash, ArticleTrash.id == fresh), 1)
        check("purge вернул ≥1", purged >= 1, True)

        await _sweep(db)

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

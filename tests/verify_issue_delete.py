"""Непустой выпуск не удаляется; статьи удаляются пачкой.

Раньше удаление выпуска открепляло его статьи (`issue_id = NULL`) и оставляло
их опубликованными. Редактор удалил номер, завёл заново и перезалил те же
статьи — каждая работа оказалась на сайте дважды. Стережём:

  * delete_issue у выпуска со статьями падает IssueNotEmptyError и ничего не
    меняет — статьи остаются в выпуске;
  * delete_articles удаляет пачку вместе с article_authors, дубли id не мешают;
  * опустевший выпуск удаляется.

Запуск: PYTHONPATH=. .venv/bin/python tests/verify_issue_delete.py
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete, func, select

from src.domain.article import ArticleDomain
from src.domain.issue import IssueDomain, IssueNotEmptyError
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    ArticleTrash,
    Issue,
    Journal,
)

GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"
results: list[bool] = []

TAG = "ISSDEL"


def check(name: str, got, want):
    ok = got == want
    results.append(ok)
    tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{tag}] {name}: got={got!r} want={want!r}")


async def _sweep(db):
    ids = (
        await db.execute(select(Article.id).where(Article.title.like(f"{TAG} %")))
    ).scalars().all()
    for stmt in (
        delete(ArticleAuthor).where(ArticleAuthor.article_id.in_(ids)),
        delete(Article).where(Article.id.in_(ids)),
        # delete_articles кладёт снимки в корзину — чистим и их.
        delete(ArticleTrash).where(ArticleTrash.title.like(f"{TAG} %")),
        delete(Issue).where(Issue.title.like(f"{TAG} %")),
        delete(Journal).where(Journal.name.like(f"{TAG} %")),
    ):
        await db.execute(stmt)
    await db.commit()


async def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    issues, articles = IssueDomain(), ArticleDomain()
    async with AsyncSessionLocal() as db:
        await _sweep(db)

        journal = Journal(name=f"{TAG} journal {suffix}", slug=f"issdel-{suffix}")
        db.add(journal)
        await db.flush()
        issue = Issue(journal_id=journal.id, title=f"{TAG} issue", year=2026)
        db.add(issue)
        await db.flush()
        a1 = Article(title=f"{TAG} первая", issue_id=issue.id, published=True)
        a2 = Article(title=f"{TAG} вторая", issue_id=issue.id, published=True)
        db.add_all([a1, a2])
        await db.flush()
        db.add(ArticleAuthor(article_id=a1.id, author_name=f"{TAG} Автор", author_order=1))
        await db.commit()
        issue_id, ids = issue.id, [a1.id, a2.id]

        print("\n--- удаление непустого выпуска ---")
        try:
            await issues.delete_issue(db, issue_id)
            check("отказ с IssueNotEmptyError", "удалён", "IssueNotEmptyError")
        except IssueNotEmptyError as err:
            check("отказ с IssueNotEmptyError", "IssueNotEmptyError", "IssueNotEmptyError")
            check("в ошибке число статей", err.count, 2)
        await db.rollback()
        left = (
            await db.execute(select(Article.issue_id).where(Article.id.in_(ids)))
        ).scalars().all()
        check("статьи остались в выпуске", sorted(left), [issue_id, issue_id])
        check("выпуск на месте", bool(await issues.get_issue(db, issue_id)), True)

        print("\n--- массовое удаление статей ---")
        deleted = await articles.delete_articles(db, ids + [ids[0]])
        check("удалено две", deleted, 2)
        rows = await db.scalar(
            select(func.count()).select_from(Article).where(Article.id.in_(ids))
        )
        check("строк статей нет", rows, 0)
        authors = await db.scalar(
            select(func.count())
            .select_from(ArticleAuthor)
            .where(ArticleAuthor.article_id.in_(ids))
        )
        check("подписи авторов удалены", authors, 0)
        check("пустой список — ноль", await articles.delete_articles(db, []), 0)

        print("\n--- удаление опустевшего выпуска ---")
        check("выпуск удалён", await issues.delete_issue(db, issue_id), True)
        check("выпуска больше нет", await issues.get_issue(db, issue_id), None)

        await _sweep(db)

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

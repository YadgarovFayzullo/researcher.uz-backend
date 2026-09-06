"""Демо-контент не подмешивается в общие цифры платформы.

Стенд для клиента живёт в проде рядом с настоящими журналами, и ему ещё и
набивают статистику (`scripts/seed_demo_stats.py`), поэтому каждая ОБЩАЯ цифра
обязана его вычитать, а каждый АДРЕСНЫЙ запрос — наоборот отдавать, иначе
клиент не увидит в своей админке собственных статей.

Что стережём:
  * get_platform_stats (счётчик просмотров на главной) демо не считает;
  * count_articles/list_articles без фильтров демо не видят, а с journal_id — видят;
  * список журналов демо не отдаёт, а с include_demo=true отдаёт;
  * аналитика по журналу (админка стенда) демо-цифры отдаёт полностью.
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete, func, select

from src.domain.article import ArticleDomain
from src.domain.demo import journal_is_not_demo
from src.domain.journal import JournalDomain
from src.domain.stats import StatsDomain
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article,
    ArticleInteraction,
    Issue,
    Journal,
)

GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"
results: list[bool] = []

TAG = "DEMOISO"

# count_articles прокидывает фильтры в _apply_filters как есть, а там все они
# keyword-only без значений по умолчанию (их всегда заполняет роутер).
NO_FILTERS = dict(
    issue_id=None,
    journal_id=None,
    publisher_id=None,
    admin_id=None,
    section_id=None,
    publication_type=None,
    field_of_science=None,
    published=None,
    has_doi=None,
    has_issue=None,
    created_after=None,
)


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
        delete(ArticleInteraction).where(ArticleInteraction.article_id.in_(ids)),
        delete(Article).where(Article.id.in_(ids)),
        delete(Issue).where(Issue.title.like(f"{TAG} %")),
        delete(Journal).where(Journal.name.like(f"{TAG} %")),
    ):
        await db.execute(stmt)
    await db.commit()


async def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as db:
        await _sweep(db)

        # ------------------------------------------------------- фикстуры
        real = Journal(name=f"{TAG} real {suffix}", slug=f"demoiso-real-{suffix}")
        demo = Journal(
            name=f"{TAG} demo {suffix}",
            slug=f"demoiso-demo-{suffix}",
            meta={"demo": True},
        )
        db.add_all([real, demo])
        await db.flush()

        i_real = Issue(journal_id=real.id, title=f"{TAG} real issue", year=2026)
        i_demo = Issue(journal_id=demo.id, title=f"{TAG} demo issue", year=2026)
        db.add_all([i_real, i_demo])
        await db.flush()

        a_real = Article(
            issue_id=i_real.id,
            title=f"{TAG} real article",
            slug=f"demoiso-real-a-{suffix}",
            published=True,
            views_count=7,
            downloads_count=3,
        )
        a_demo = Article(
            issue_id=i_demo.id,
            title=f"{TAG} demo article",
            slug=f"demoiso-demo-a-{suffix}",
            published=True,
            views_count=500,
            downloads_count=100,
        )
        db.add_all([a_real, a_demo])
        await db.commit()

        arts = ArticleDomain()
        journals = JournalDomain()

        # ------------------------------------------- общие цифры платформы
        print("\n--- общие цифры ---")
        base = await StatsDomain.get_platform_stats(db)

        # Счётчик выпусков на главной: настоящий выпуск в нём есть, демо — нет.
        # Меряем разницей с тем же счётчиком без обоих фикстурных выпусков
        # (по id, а не по названию: у выпусков платформы title часто NULL, а
        # `NOT LIKE` от NULL даёт NULL и вырезал бы их из базы сравнения).
        no_fixture = (
            await db.execute(
                select(func.count(Issue.id))
                .join(Journal, Journal.id == Issue.journal_id)
                .where(
                    journal_is_not_demo(),
                    Journal.type != "conference_series",
                    ~Issue.meta.has_key("blocked"),  # noqa: W601
                    Issue.id.notin_([i_real.id, i_demo.id]),
                )
            )
        ).scalar_one()
        check("демо-выпуск в счётчик главной не попадает", base["totalIssues"] - no_fixture, 1)

        a_demo.views_count = 900
        a_demo.downloads_count = 300
        await db.commit()
        after_demo = await StatsDomain.get_platform_stats(db)
        check("демо-просмотры в сумму не попадают", after_demo["totalViews"], base["totalViews"])
        check("демо-скачивания в сумму не попадают", after_demo["totalDownloads"], base["totalDownloads"])

        a_real.views_count = 11
        await db.commit()
        after_real = await StatsDomain.get_platform_stats(db)
        check(
            "настоящие просмотры в сумму попадают",
            after_real["totalViews"] - base["totalViews"],
            4,
        )

        # --------------------------------------------- ленты и счётчики
        print("\n--- ленты ---")
        _, total_all = await arts.list_articles(db, q=TAG, limit=100)
        check("общая лента демо не отдаёт", total_all, 1)
        check(
            "count без фильтров демо не считает",
            await arts.count_articles(db, q=TAG, **NO_FILTERS),
            1,
        )
        check(
            "адресный count по демо-журналу видит статью",
            await arts.count_articles(db, **{**NO_FILTERS, "journal_id": [demo.id]}, q=TAG),
            1,
        )

        print("\n--- журналы ---")
        listed = {j["id"] for j in await journals.list_journals(db)}
        check("демо-журнала нет в общем списке", demo.id in listed, False)
        check("настоящий журнал в списке есть", real.id in listed, True)
        with_demo = {
            j["id"] for j in await journals.list_journals(db, include_demo=True)
        }
        check("include_demo отдаёт демо-журнал", demo.id in with_demo, True)

        # ------------------------------------- админка стенда не пострадала
        print("\n--- админка стенда ---")
        analytics = await StatsDomain.get_journal_analytics(db, [demo.id])
        check("аналитика демо-журнала отдаёт его просмотры", analytics[0]["total_views"], 900)
        overview = {
            r["journal_id"]: r
            for r in await StatsDomain.get_journals_overview(db)
        }
        check("обзор по журналам знает демо-журнал", overview[demo.id]["total_views"], 900)

        # ------------------------------------------------------- уборка
        print("\n--- уборка ---")
        await _sweep(db)
        left = (
            await db.execute(select(Article).where(Article.title.like(f"{TAG} %")))
        ).scalars().all()
        check("остатков фикстур не осталось", len(left), 0)

    total_n, ok_n = len(results), sum(results)
    print(f"\n{'='*46}\nИтог: {ok_n}/{total_n} " + ("— всё зелёное" if ok_n == total_n else "— ЕСТЬ ПАДЕНИЯ"))
    return 0 if ok_n == total_n else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

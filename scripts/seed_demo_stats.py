"""Наполнить демо-журнал правдоподобной статистикой просмотров и скачиваний.

Нужен для стендов, которые показывают клиенту: свежесозданный журнал даёт в
панели аналитики одни нули, и посмотреть там нечего. Скрипт пишет события в
`article_interactions` (источник истины для всех агрегатов в `src/domain/stats.py`)
и приводит к ним денормализованные `articles.views_count/downloads_count`.

События раскидываются по последним N дням со смещением к сегодняшнему дню, так
что окна 7/30/90 в панели все непустые, а дневной график имеет форму. Просмотры
распределяются между статьями неравномерно — иначе «топ статей» выглядит мёртвым.

Работает ТОЛЬКО с журналом, у которого `metadata.demo = true`: подрисовывать
статистику настоящему журналу скрипт откажется.

    ssh ubuntu@<host> "cd ~/app && sudo docker compose -f docker-compose.prod.yml \\
        exec -T -e PYTHONPATH=/app api python scripts/seed_demo_stats.py \\
        --journal research-focus --views 487 --downloads 130"

`--reset` стирает ранее засеянную статистику этого журнала и пишет заново —
иначе повторный запуск просто добавит событий поверх.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article,
    ArticleInteraction,
    Issue,
    Journal,
)


def split_weighted(total: int, parts: int) -> list[int]:
    """Разложить total на parts убывающих долей (zipf-подобно): у первой статьи
    просмотров заметно больше, чем у последней — так выглядит живой журнал."""
    if parts <= 0 or total <= 0:
        return [0] * max(parts, 0)
    weights = [1 / (i + 1) ** 0.9 for i in range(parts)]
    s = sum(weights)
    out = [int(total * w / s) for w in weights]
    out[0] += total - sum(out)  # остаток от округления — самой популярной
    return out


def spread(count: int, days: int) -> list[datetime]:
    """Моменты событий за последние `days` дней со смещением к недавним."""
    now = datetime.now(timezone.utc)
    return [
        now - timedelta(days=random.random() ** 1.6 * days, hours=random.random() * 24)
        for _ in range(count)
    ]


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--journal", required=True, help="слаг или id демо-журнала")
    p.add_argument("--views", type=int, default=487)
    p.add_argument("--downloads", type=int, default=130)
    p.add_argument("--days", type=int, default=90, help="на сколько дней назад разбрасывать")
    p.add_argument("--reset", action="store_true", help="сначала стереть прежнюю статистику")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    async with AsyncSessionLocal() as db:
        q = select(Journal)
        q = q.where(
            Journal.id == int(args.journal)
            if args.journal.isdigit()
            else Journal.slug == args.journal
        )
        journal = (await db.execute(q)).scalars().first()
        if journal is None:
            raise SystemExit(f"Журнал {args.journal!r} не найден")
        if not (journal.meta or {}).get("demo"):
            raise SystemExit(
                f"У журнала {journal.slug!r} нет metadata.demo — "
                "подрисовывать статистику настоящему журналу нельзя"
            )

        articles = (
            await db.execute(
                select(Article.id, Article.title)
                .join(Issue, Issue.id == Article.issue_id)
                .where(Issue.journal_id == journal.id)
                .order_by(Article.id)
            )
        ).all()
        if not articles:
            raise SystemExit("У журнала нет статей — сначала создай их (create_demo.py)")

        views = split_weighted(args.views, len(articles))
        downloads = split_weighted(args.downloads, len(articles))

        print(f"Журнал {journal.slug} (id={journal.id}), статей: {len(articles)}")
        for (aid, title), v, d in zip(articles, views, downloads):
            print(f"  · {(title or '')[:52]:<54} {v:>4} просм. {d:>4} скач.")
        print(f"Итого: {sum(views)} просмотров, {sum(downloads)} скачиваний "
              f"за последние {args.days} дн.")
        if args.dry_run:
            print("\n--dry-run: ничего не записано.")
            return 0

        ids = [aid for aid, _ in articles]
        if args.reset:
            removed = (
                await db.execute(
                    delete(ArticleInteraction).where(ArticleInteraction.article_id.in_(ids))
                )
            ).rowcount
            print(f"- удалено прежних событий: {removed}")

        for (aid, _), v, d in zip(articles, views, downloads):
            for ts in spread(v, args.days):
                db.add(ArticleInteraction(id=uuid.uuid4(), article_id=aid, view=1, created_at=ts))
            for ts in spread(d, args.days):
                db.add(
                    ArticleInteraction(id=uuid.uuid4(), article_id=aid, download=1, created_at=ts)
                )
            # Счётчики держим в синхроне с логом: горячие чтения статистики
            # берут их, а не агрегат по article_interactions.
            await db.execute(
                update(Article)
                .where(Article.id == aid)
                .values(
                    views_count=v if args.reset else Article.views_count + v,
                    downloads_count=d if args.reset else Article.downloads_count + d,
                )
            )

        await db.commit()

    print("\nГотово. Панель: /admin/analytics")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

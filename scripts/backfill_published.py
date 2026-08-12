"""Проставить published=true статьям, созданным до появления флага.

У колонки в БД default false, а формы его не выставляли — поэтому часть статей
лежит с published=false, хотя всё это время была видна на сайте (фильтра нигде
не было). Теперь фильтр включён, и без этого прогона такие статьи разом
исчезли бы из каталогов, поиска и карты сайта.

Прогонять ОДИН раз, вместе с выкаткой фильтра.

    python scripts/backfill_published.py --dry-run   # показать, скольких коснётся
    python scripts/backfill_published.py             # проставить
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select, update  # noqa: E402

from src.infrastructure.persistence.db import AsyncSessionLocal  # noqa: E402
from src.infrastructure.persistence.models import Article  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill_published")


async def main(dry_run: bool) -> int:
    async with AsyncSessionLocal() as db:
        unpublished = (
            await db.execute(
                select(func.count(Article.id)).where(
                    (Article.published.is_(False)) | (Article.published.is_(None))
                )
            )
        ).scalar_one()
        total = (await db.execute(select(func.count(Article.id)))).scalar_one()

        logger.info("Всего статей:        %d", total)
        logger.info("Без published=true:  %d", unpublished)

        if dry_run:
            logger.info("\n--dry-run: ничего не изменено.")
            return 0
        if not unpublished:
            logger.info("Нечего проставлять.")
            return 0

        await db.execute(
            update(Article)
            .where((Article.published.is_(False)) | (Article.published.is_(None)))
            .values(published=True)
        )
        await db.commit()
        logger.info("Проставлено published=true: %d", unpublished)
        return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(args.dry_run)))

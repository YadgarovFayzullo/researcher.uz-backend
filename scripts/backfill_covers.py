"""Догенерировать обложки изданиям, у которых есть PDF, но нет cover_image.

Разовый прогон после включения серверной генерации: старые записи заводились,
когда обложку рисовал браузер через pdf.js, и в БД у них пусто.

    python scripts/backfill_covers.py --dry-run       # только показать список
    python scripts/backfill_covers.py                 # все издания
    python scripts/backfill_covers.py --limit 5       # первые 5 (проверить)
    python scripts/backfill_covers.py --include-articles

По умолчанию берём только самостоятельные издания (монографии, диссертации,
учебники и т.д.) — именно их карточки показывают обложку. Статьи журналов
выводятся списком без обложек, им это не нужно; при желании — флагом.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from src.infrastructure.covers import generate_cover_from_pdf  # noqa: E402
from src.infrastructure.persistence.db import AsyncSessionLocal  # noqa: E402
from src.infrastructure.persistence.models import Article  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill_covers")


async def main(limit: int | None, dry_run: bool, include_articles: bool) -> int:
    async with AsyncSessionLocal() as db:
        query = select(Article).where(
            Article.pdf.isnot(None),
            Article.pdf != "",
            (Article.cover_image.is_(None)) | (Article.cover_image == ""),
        )
        if not include_articles:
            query = query.where(
                Article.publication_type.isnot(None),
                Article.publication_type != "article",
            )
        query = query.order_by(Article.id)
        if limit:
            query = query.limit(limit)

        rows = (await db.execute(query)).scalars().all()
        logger.info("Кандидатов без обложки: %d", len(rows))

        if dry_run:
            for a in rows:
                logger.info("  [%s] %s", a.id, a.title or a.slug)
            return 0

        done = failed = 0
        for a in rows:
            url = await asyncio.to_thread(
                generate_cover_from_pdf, a.pdf, a.title or a.slug or "cover"
            )
            if not url:
                failed += 1
                logger.warning("  ✗ [%s] %s", a.id, a.title or a.slug)
                continue
            a.cover_image = url
            # Коммитим по одной: прогон долгий, и обрыв на середине не должен
            # терять уже сделанную работу.
            await db.commit()
            done += 1
            logger.info("  ✓ [%s] %s", a.id, url)

        logger.info("Готово: обложек создано %d, не удалось %d", done, failed)
        return 1 if failed and not done else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--include-articles",
        action="store_true",
        help="включить и статьи журналов, не только самостоятельные издания",
    )
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(main(args.limit, args.dry_run, args.include_articles))
    )

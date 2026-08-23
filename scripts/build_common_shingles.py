"""Пересчёт стоп-листа шаблонных фраз.

Шингл, встречающийся в слишком многих статьях, — это не заимствование, а
колонтитул («Ilmiy-nazariy va metodik jurnal», ISSN, номер выпуска) или
канцелярский оборот. Такие шинглы попадают в `common_fingerprints` и при
проверке не учитываются вовсе — ни в совпадениях, ни в знаменателе процента.

Порог по умолчанию — 0,5% проиндексированных статей, но не меньше 10: на базе
в 2700 статей это 13 работ. Фраза, встреченная в тринадцати разных статьях
разных авторов, почти наверняка шаблон.

    PYTHONPATH=. .venv/bin/python scripts/build_common_shingles.py
    ... --min-articles 25     # задать порог вручную
    ... --show                # посмотреть примеры, ничего не записывая
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import delete, func, select, text

from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    ArticleFingerprint,
    ArticleText,
    CommonFingerprint,
)

DEFAULT_SHARE = 0.005
MIN_THRESHOLD = 10


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--min-articles", type=int, help="порог по числу статей")
    p.add_argument("--show", action="store_true", help="только показать, не записывать")
    args = p.parse_args()

    async with AsyncSessionLocal() as db:
        indexed = int(
            (
                await db.execute(
                    select(func.count(ArticleText.article_id)).where(
                        ArticleText.status == "ok"
                    )
                )
            ).scalar_one()
        )
        threshold = args.min_articles or max(MIN_THRESHOLD, int(indexed * DEFAULT_SHARE))
        print(f"проиндексировано статей: {indexed}, порог: {threshold}")

        rows = (
            await db.execute(
                select(
                    ArticleFingerprint.hash,
                    func.count(func.distinct(ArticleFingerprint.article_id)).label("cnt"),
                )
                .group_by(ArticleFingerprint.hash)
                .having(func.count(func.distinct(ArticleFingerprint.article_id)) >= threshold)
            )
        ).all()
        print(f"шаблонных шинглов: {len(rows)}")

        if args.show:
            for hash_value, count in sorted(rows, key=lambda r: -r[1])[:10]:
                sample = (
                    await db.execute(
                        text(
                            "select substring(t.content from greatest(1, f.position * 6 - 30) for 70) "
                            "from article_fingerprints f join article_texts t on t.article_id = f.article_id "
                            "where f.hash = :h limit 1"
                        ),
                        {"h": hash_value},
                    )
                ).scalar()
                snippet = " ".join((sample or "").split())[:70]
                print(f"  {count:>5} статей — «{snippet}…»")
            return 0

        await db.execute(delete(CommonFingerprint))
        CHUNK = 5000
        for start in range(0, len(rows), CHUNK):
            chunk = rows[start : start + CHUNK]
            if chunk:
                await db.execute(
                    CommonFingerprint.__table__.insert(),
                    [{"hash": h, "articles_count": int(c)} for h, c in chunk],
                )
        await db.commit()
        print(f"записано в стоп-лист: {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

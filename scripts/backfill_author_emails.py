"""Почта авторов из уже сохранённых текстов статей.

Новые статьи получают её сами: разбор врезан в `PlagiarismDomain.index_article`,
через который проходят импорт, папка PDF и переиндексация. Этот скрипт — разовый
добор по тем статьям, чей текст уже лежит в `article_texts`.

Без --apply ничего не пишет: печатает, сколько адресов нашлось и скольким
подписям они достанутся.

    docker exec app-api-1 python scripts/backfill_author_emails.py
    docker exec app-api-1 python scripts/backfill_author_emails.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from src.domain.author_contacts import attach_article_contacts  # noqa: E402
from src.infrastructure.persistence.db import AsyncSessionLocal  # noqa: E402
from src.infrastructure.persistence.models import ArticleAuthor, ArticleText  # noqa: E402

CHUNK = 200


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="записать найденные адреса")
    ap.add_argument("--limit", type=int, default=0, help="только первые N статей")
    ap.add_argument(
        "--overwrite", action="store_true", help="перезаписать уже проставленные адреса"
    )
    args = ap.parse_args()

    async with AsyncSessionLocal() as db:
        ids = list(
            (
                await db.execute(
                    select(ArticleText.article_id)
                    .where(ArticleText.content.like("%@%"))
                    .order_by(ArticleText.article_id)
                    .limit(args.limit or None)
                )
            ).scalars()
        )
        before = (
            await db.execute(
                select(func.count()).select_from(ArticleAuthor).where(ArticleAuthor.email.isnot(None))
            )
        ).scalar_one()

    print(f"статей с '@' в тексте: {len(ids)}; подписей с адресом уже: {before}")

    totals = {"contacts": 0, "matched": 0, "written": 0, "articles": 0}
    for start in range(0, len(ids), CHUNK):
        batch = ids[start:start + CHUNK]
        async with AsyncSessionLocal() as db:
            for article_id in batch:
                res = await attach_article_contacts(db, article_id, overwrite=args.overwrite)
                for k, v in res.items():
                    totals[k] += v
                if res["written"]:
                    totals["articles"] += 1
            if args.apply:
                await db.commit()
            else:
                await db.rollback()
        print(
            f"  {min(start + CHUNK, len(ids))}/{len(ids)}: адресов {totals['contacts']}, "
            f"привязано {totals['matched']}, к записи {totals['written']}",
            flush=True,
        )

    async with AsyncSessionLocal() as db:
        uniq = (
            await db.execute(
                select(func.count(func.distinct(func.lower(ArticleAuthor.email)))).where(
                    ArticleAuthor.email.isnot(None)
                )
            )
        ).scalar_one()
    verb = "записано" if args.apply else "было бы записано"
    print(
        f"\n{verb}: {totals['written']} подписей в {totals['articles']} статьях; "
        f"уникальных адресов в базе: {uniq}"
    )
    if not args.apply:
        print("Просмотр. Для записи добавьте --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

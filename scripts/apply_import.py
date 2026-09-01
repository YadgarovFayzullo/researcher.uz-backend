"""Применить задачу импорта из командной строки: выпуски, статьи, PDF.

То же самое, что кнопка «Применить» в мастере (`ImportDomain.apply`), но без
браузера: прогон длинный — на каждую статью заход на страницу источника и
скачивание файла, — а держать вкладку открытой полчаса незачем.

`--publish` дополнительно снимает черновой статус с созданных статей. По
умолчанию импорт кладёт их с `published = false`: чужие данные грязные, и
показывать их читателю до проверки нельзя. Флаг нужен, когда архив уже
просмотрен и его сразу ставят в витрину журнала.

Пример:
    IMPORT_MIN_INTERVAL=3 python scripts/apply_import.py --job-id 11
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select, update

from src.domain.importing import ImportDomain
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import Article, ImportItem, ImportJob, Issue


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="снять черновой статус с созданных статей (по умолчанию остаются черновиками)",
    )
    args = parser.parse_args()

    domain = ImportDomain()
    async with AsyncSessionLocal() as db:
        job = (
            await db.execute(select(ImportJob).where(ImportJob.id == args.job_id))
        ).scalar_one()
        pending = (
            await db.execute(
                select(ImportItem.status, ImportItem.id).where(ImportItem.job_id == job.id)
            )
        ).all()
        print(f"задача #{job.id}, журнал {job.journal_id}, кандидатов {len(pending)}", flush=True)

        report = await domain.apply(db, job)
        print(f"создано: {report.get('created')}, не удалось: {report.get('failed')}", flush=True)

        created_ids = (
            (
                await db.execute(
                    select(ImportItem.article_id).where(
                        ImportItem.job_id == job.id, ImportItem.status == "created"
                    )
                )
            )
            .scalars()
            .all()
        )
        created_ids = [i for i in created_ids if i]
        print(f"статей создано всего по задаче: {len(created_ids)}", flush=True)

        if created_ids and args.publish:
            await db.execute(
                update(Article).where(Article.id.in_(created_ids)).values(published=True)
            )
            await db.commit()
            print(f"опубликовано: {len(created_ids)}", flush=True)

        rows = (
            await db.execute(
                select(Issue.id, Issue.year, Issue.volume, Issue.issue)
                .where(Issue.journal_id == job.journal_id, Issue.year == 2023)
                .order_by(Issue.id)
            )
        ).all()
        print("выпуски 2023 в журнале:", [(r.id, r.volume, r.issue) for r in rows], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

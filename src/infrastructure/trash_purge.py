"""Фоновая чистка корзины статей: записи старше 30 дней удаляются.

Отдельно от scheduler.py: тот стартует только при включённой ИИ-проверке, а
корзина должна чиститься всегда. Advisory-lock не нужен — DELETE по сроку
идемпотентен, и два воркера, прошедшие его одновременно, ничего не сломают.
"""
from __future__ import annotations

import asyncio
import logging

from src.domain import article_trash
from src.infrastructure.persistence.db import AsyncSessionLocal

logger = logging.getLogger(__name__)

STARTUP_DELAY_SECONDS = 60
INTERVAL_SECONDS = 6 * 3600


async def run_forever() -> None:
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                purged = await article_trash.purge_expired(db)
            if purged:
                logger.info("article_trash: удалено просроченных записей: %s", purged)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("article_trash: чистка корзины упала")
        await asyncio.sleep(INTERVAL_SECONDS)


def start(app) -> None:
    app.state.trash_purge_task = asyncio.create_task(run_forever())


async def stop(app) -> None:
    task = getattr(app.state, "trash_purge_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

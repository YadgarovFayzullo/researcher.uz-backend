"""Фоновый планировщик ИИ-проверки выпусков.

Отдельного планировщика (cron, celery, apscheduler) в проекте нет и заводить
его ради одной задачи незачем: тик раз в пять минут — это asyncio-цикл внутри
приложения, а состояние очереди лежит в базе (`articles.metadata.ai_review`),
поэтому рестарт контейнера ничего не теряет.

Две вещи, без которых это ломается в проде:

* **Воркеров два** (`WEB_CONCURRENCY=2` в docker-entrypoint.sh), и lifespan
  выполняется в каждом. Без взаимного исключения два процесса проверяли бы один
  выпуск одновременно и платили бы за модель дважды. Отсюда advisory-lock
  Postgres: у кого получилось — тот и работает, второй молча пропускает тик.
* **Лок держится на отдельном соединении.** Сессия отдаёт коннект в пул на
  каждом commit, а сессионный advisory-lock живёт ровно столько, сколько живёт
  захвативший его коннект: возьми мы лок через рабочую сессию, он снялся бы
  посреди прогона.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from src.core.config import settings
from src.domain import ai_review
from src.infrastructure.persistence.db import AsyncSessionLocal, engine

logger = logging.getLogger(__name__)

# Произвольная константа — просто имя лока, общее для всех воркеров.
LOCK_KEY = 815_240_001

# Первый тик не сразу после старта: при выкатке контейнеры поднимаются один за
# другим, и незачем ломиться в базу в тот же миг.
STARTUP_DELAY_SECONDS = 30


async def _tick() -> None:
    async with engine.connect() as conn:
        locked = (
            await conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}
            )
        ).scalar()
        if not locked:
            return
        try:
            async with AsyncSessionLocal() as db:
                issue_ids = await ai_review.due_issue_ids(db)
                for issue_id in issue_ids:
                    result = await ai_review.review_issue(db, issue_id)
                    logger.info("ai_review: выпуск %s → %s", issue_id, result)
        finally:
            await conn.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY}
            )


async def run_forever() -> None:
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Цикл не имеет права умереть: упавший тик — это отложенная
            # проверка, а прекратившийся планировщик — навсегда непубликуемые
            # черновики, чего никто не заметит до жалобы редактора.
            logger.exception("ai_review: тик планировщика упал")
        await asyncio.sleep(max(60, settings.AI_REVIEW_POLL_SECONDS))


def start(app) -> None:
    """Запустить планировщик, если ИИ-проверка включена ключом."""
    if not settings.REVIEW_ACTIVE:
        logger.info("ai_review: автопроверка выключена — планировщик не запущен")
        return
    app.state.ai_review_task = asyncio.create_task(run_forever())
    logger.info(
        "ai_review: планировщик запущен (пауза %s мин, опрос каждые %s сек)",
        settings.AI_REVIEW_DELAY_MINUTES,
        settings.AI_REVIEW_POLL_SECONDS,
    )


async def stop(app) -> None:
    task = getattr(app.state, "ai_review_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

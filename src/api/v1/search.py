"""Поиск — эндпоинт (Фаза 7, порт RPC search_articles)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.ratelimit import limiter
from src.core.turnstile import require_human
from src.domain.search import SearchDomain
from src.infrastructure.persistence.db import get_db

router = APIRouter()
domain = SearchDomain()


@router.get("", dependencies=[Depends(require_human)])
@limiter.limit("60/minute")
async def search(
    request: Request,
    q: str = Query("", description="Поисковый запрос"),
    db: AsyncSession = Depends(get_db),
):
    """Полнотекстовый поиск статей. Плоский список, ранжированный по релевантности
    (как RPC search_articles); фронт пагинирует клиентски.

    Закрыт от ботов: нужен cookie-пропуск Turnstile или сессия (require_human).
    Пока TURNSTILE_SECRET_KEY не задан, проверка не действует."""
    results = await domain.search_articles(db, q)
    return {"query": q, "count": len(results), "results": results}

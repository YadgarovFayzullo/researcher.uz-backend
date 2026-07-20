"""Поиск — эндпоинт (Фаза 7, порт RPC search_articles)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.search import SearchDomain
from src.infrastructure.persistence.db import get_db

router = APIRouter()
domain = SearchDomain()


@router.get("")
async def search(
    q: str = Query("", description="Поисковый запрос"),
    db: AsyncSession = Depends(get_db),
):
    """Полнотекстовый поиск статей. Плоский список, ранжированный по релевантности
    (как RPC search_articles); фронт пагинирует клиентски."""
    results = await domain.search_articles(db, q)
    return {"query": q, "count": len(results), "results": results}

"""Цитирования — эндпоинты (Фаза 5, порт citations.sql RPC).

Публичные (аноним): counts / citing / match-doi.
Owner-only: external (upsert кэша OpenAlex) — двойной гард (require_owner +
caller_is_owner в сервисе).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import require_owner
from src.domain.citations import CitationsDomain
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Profile
from src.schemas.citations import (
    ArticleIdsRequest,
    CitationCount,
    CitingArticle,
    DoiMatch,
    DoisRequest,
    RefreshRequest,
    UpsertExternalRequest,
)

router = APIRouter()
domain = CitationsDomain()


@router.post("/counts", response_model=list[CitationCount])
async def article_citations(
    body: ArticleIdsRequest, db: AsyncSession = Depends(get_db)
):
    return await domain.get_article_citations(db, body.article_ids)


@router.post("/years")
async def citation_years(body: ArticleIdsRequest, db: AsyncSession = Depends(get_db)):
    """[{year, count}] — сумма внешних цитирований по годам для набора статей."""
    return await domain.get_citation_years(db, body.article_ids)


@router.post("/breakdown")
async def citation_breakdown(
    body: ArticleIdsRequest, db: AsyncSession = Depends(get_db)
):
    """{external:[{article_id,year,count}], internal:[{cited_article_id,year,count}]}."""
    return await domain.get_citation_breakdown(db, body.article_ids)


@router.get("/citing/{article_id}", response_model=list[CitingArticle])
async def citing_articles(article_id: int, db: AsyncSession = Depends(get_db)):
    return await domain.get_citing_articles(db, article_id)


@router.post("/match-doi", response_model=list[DoiMatch])
async def match_doi(body: DoisRequest, db: AsyncSession = Depends(get_db)):
    return await domain.match_articles_by_doi(db, body.dois)


@router.post("/external")
async def upsert_external(
    body: UpsertExternalRequest,
    db: AsyncSession = Depends(get_db),
    _owner: Profile = Depends(require_owner),
):
    rows = [r.model_dump() for r in body.rows]
    n = await domain.upsert_external_citations(db, caller_is_owner=True, rows=rows)
    return {"upserted": n}


@router.post("/refresh")
async def refresh_openalex(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    _owner: Profile = Depends(require_owner),
):
    """Обновить кэш цитирований из OpenAlex (owner-only, порт /api/citations/refresh)."""
    return await domain.refresh_from_openalex(
        db, caller_is_owner=True, limit=body.limit, offset=body.offset
    )

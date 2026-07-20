"""Под-ресурсы статьи: авторы и пристатейные ссылки.

Монтируется под `/articles`, поэтому пути выглядят как
`/articles/{article_id}/authors` и `/articles/{article_id}/references`.
Чтение публично; запись требует права на саму статью (`can_write_article`).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_profile
from src.domain.article import ArticleDomain
from src.domain.authz import can_write_article
from src.domain.content import AuthorDomain, ReferenceDomain
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Profile
from src.schemas.content import (
    AuthorPublic,
    AuthorsReplace,
    ReferencePublic,
    ReferencesReplace,
)

router = APIRouter()
authors = AuthorDomain()
references = ReferenceDomain()
articles = ArticleDomain()


async def _guard_article(db: AsyncSession, profile: Profile, article_id: int) -> None:
    article = await articles.get_article_by_id(db, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    if not await can_write_article(
        db,
        role=profile.role,
        user_id=profile.id,
        issue_id=article.issue_id,
        admin_id=article.admin_id,
        publisher_id=article.publisher_id,
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Not allowed to write this article"
        )


# ------------------------------------------------------------------- авторы
@router.get("/{article_id}/authors", response_model=list[AuthorPublic])
async def list_authors(article_id: int, db: AsyncSession = Depends(get_db)):
    return await authors.list_by_article(db, article_id)


@router.put("/{article_id}/authors", response_model=list[AuthorPublic])
async def replace_authors(
    article_id: int,
    payload: AuthorsReplace,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    await _guard_article(db, profile, article_id)
    return await authors.replace_for_article(db, article_id, payload.authors)


# ------------------------------------------------------------------- ссылки
@router.get("/{article_id}/references", response_model=list[ReferencePublic])
async def list_references(article_id: int, db: AsyncSession = Depends(get_db)):
    return await references.list_by_article(db, article_id)


@router.put("/{article_id}/references", response_model=list[ReferencePublic])
async def replace_references(
    article_id: int,
    payload: ReferencesReplace,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    await _guard_article(db, profile, article_id)
    return await references.replace_for_article(db, article_id, payload.references)


class CitingRequest(BaseModel):
    cited_article_ids: list[int]


@router.post("/references/citing")
async def citing_map(payload: CitingRequest, db: AsyncSession = Depends(get_db)):
    """Кто ссылается на указанные статьи: {cited_article_id: [article_id, ...]}.

    Батч — CitationsDashboard запрашивает срезами, чтобы не упереться в лимит
    длины URL.
    """
    result = await references.citing_map(db, payload.cited_article_ids)
    return {str(k): v for k, v in result.items()}

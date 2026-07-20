"""Библиотека пользователя (`/library`) — сохранённые статьи.

Всё под аутентификацией: строки принадлежат пользователю (RLS saved_articles.sql
разрешает видеть и менять только свои).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_profile
from src.domain.content import LibraryDomain
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Profile
from src.schemas.content import SavedState

router = APIRouter()
domain = LibraryDomain()


@router.get("/")
async def list_saved(
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    return await domain.list_saved(db, profile.id)


@router.get("/{article_id}", response_model=SavedState)
async def is_saved(
    article_id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    return SavedState(saved=await domain.is_saved(db, profile.id, article_id))


@router.post("/{article_id}", response_model=SavedState, status_code=201)
async def save_article(
    article_id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    await domain.save(db, profile.id, article_id)
    return SavedState(saved=True)


@router.delete("/{article_id}", response_model=SavedState)
async def unsave_article(
    article_id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    await domain.unsave(db, profile.id, article_id)
    return SavedState(saved=False)

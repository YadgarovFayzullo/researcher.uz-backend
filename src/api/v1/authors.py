"""Карточки авторов — публичные страницы /author/<slug> и присвоение.

Читать может кто угодно: карточка адресована в том числе поисковикам, ради
которых она и заводится. Писать (claim) — только под своей сессией.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_profile
from src.domain.authors import AuthorCardDomain, AuthorCardError
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Profile

router = APIRouter()
domain = AuthorCardDomain()


@router.get("/")
async def list_authors(
    limit: int = 100, offset: int = 0, db: AsyncSession = Depends(get_db)
):
    """Индексируемые карточки — перелинковка и карта сайта."""
    return await domain.list_top(db, limit=limit, offset=offset)


@router.get("/{slug}")
async def author_card(slug: str, db: AsyncSession = Depends(get_db)):
    card = await domain.get_by_slug(db, slug)
    if card is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Author not found")
    card["publications"] = await domain.publications(db, card["id"])
    card["name_variants"] = await domain.name_variants(db, card["id"])
    return card


@router.post("/{slug}/claim")
async def claim_author(
    slug: str,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    try:
        return await domain.claim(db, slug, str(profile.id))
    except AuthorCardError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err

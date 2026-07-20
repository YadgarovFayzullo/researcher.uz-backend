"""Издатели (`/publishers`).

Чтение публично. Создание/удаление — owner. Редактирование карточки — owner
или прикреплённый админ (`publishers.admin_id`), как в middleware.ts фронта и
в RLS publishers.sql.

Порядок объявления важен: литеральные пути (`/mine`, `/by-id/...`) должны идти
до `/{slug}`, иначе он перехватит их как slug.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_profile, require_owner
from src.domain.authz import is_owner
from src.domain.publisher import PublisherDomain
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Profile
from src.schemas.publisher import PublisherCreate, PublisherPublic, PublisherUpdate

router = APIRouter()
domain = PublisherDomain()


@router.get("/", response_model=list[PublisherPublic])
async def list_publishers(db: AsyncSession = Depends(get_db)):
    return await domain.list_publishers(db)


@router.get("/mine", response_model=list[PublisherPublic])
async def list_my_publishers(
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    """Издатели текущего админа (/admin/publishers). Owner видит всех."""
    if is_owner(profile.role):
        return await domain.list_publishers(db)
    return await domain.list_publishers(db, admin_id=profile.id)


@router.get("/by-id/{publisher_id}", response_model=PublisherPublic)
async def get_publisher_by_id(publisher_id: int, db: AsyncSession = Depends(get_db)):
    """Админка адресует издателя по id (/admin/publishers/<id>), публика — по slug."""
    publisher = await domain.get_by_id(db, publisher_id)
    if not publisher:
        raise HTTPException(status_code=404, detail="Publisher not found")
    return publisher


@router.get("/{slug}", response_model=PublisherPublic)
async def get_publisher(slug: str, db: AsyncSession = Depends(get_db)):
    publisher = await domain.get_by_slug(db, slug)
    if not publisher:
        raise HTTPException(status_code=404, detail="Publisher not found")
    return publisher


@router.post("/", response_model=PublisherPublic, status_code=201)
async def create_publisher(
    publisher_in: PublisherCreate,
    admin_id: str | None = Query(
        None, description="кому прикрепить (по умолчанию — создателю)"
    ),
    db: AsyncSession = Depends(get_db),
    owner: Profile = Depends(require_owner),
):
    if await domain.get_by_slug(db, publisher_in.slug):
        raise HTTPException(status_code=409, detail="Publisher slug already exists")
    return await domain.create_publisher(db, publisher_in, admin_id=admin_id or owner.id)


@router.patch("/{publisher_id}", response_model=PublisherPublic)
async def update_publisher(
    publisher_id: int,
    publisher_in: PublisherUpdate,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    existing = await domain.get_by_id(db, publisher_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Publisher not found")
    owner = is_owner(profile.role)
    if not owner and existing.admin_id != profile.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Not allowed to edit this publisher"
        )
    return await domain.update_publisher(
        db, publisher_id, publisher_in, allow_admin_change=owner
    )


@router.delete("/{publisher_id}")
async def delete_publisher(
    publisher_id: int,
    db: AsyncSession = Depends(get_db),
    _owner: Profile = Depends(require_owner),
):
    if not await domain.delete_publisher(db, publisher_id):
        raise HTTPException(status_code=404, detail="Publisher not found")
    return {"status": "deleted"}

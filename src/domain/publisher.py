"""Издатели (`publishers`) — см. supabase/publishers.sql.

Издатель = контейнер для самостоятельных изданий (монографии, диссертации,
учебники): публичная страница /publisher/<slug>, админ-воркспейс
/admin/publishers/<id>. Связь с материалами — articles.publisher_id.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.persistence.models import Article, Publisher
from src.schemas.publisher import PublisherCreate, PublisherUpdate


class PublisherDomain:
    async def list_publishers(
        self, db: AsyncSession, *, admin_id=None
    ) -> list[Publisher]:
        """Все издатели (PublishersList) либо только свои (админ-режим)."""
        stmt = select(Publisher)
        if admin_id is not None:
            stmt = stmt.where(Publisher.admin_id == admin_id)
        res = await db.execute(stmt.order_by(Publisher.name))
        return list(res.scalars().all())

    async def get_by_slug(self, db: AsyncSession, slug: str) -> Publisher | None:
        res = await db.execute(select(Publisher).where(Publisher.slug == slug))
        return res.scalars().first()

    async def get_by_id(self, db: AsyncSession, publisher_id: int) -> Publisher | None:
        res = await db.execute(select(Publisher).where(Publisher.id == publisher_id))
        return res.scalars().first()

    async def create_publisher(
        self, db: AsyncSession, publisher_in: PublisherCreate, *, admin_id=None
    ) -> Publisher:
        publisher = Publisher(**publisher_in.model_dump(), admin_id=admin_id)
        db.add(publisher)
        await db.commit()
        await db.refresh(publisher)
        return publisher

    async def update_publisher(
        self,
        db: AsyncSession,
        publisher_id: int,
        publisher_in: PublisherUpdate,
        *,
        allow_admin_change: bool,
    ) -> Publisher | None:
        publisher = await self.get_by_id(db, publisher_id)
        if not publisher:
            return None
        data = publisher_in.model_dump(exclude_unset=True)
        # Переназначать владельца карточки может только owner — иначе админ мог бы
        # передать издателя кому угодно (или отобрать у себя доступ).
        if not allow_admin_change:
            data.pop("admin_id", None)
        for field, value in data.items():
            setattr(publisher, field, value)
        await db.commit()
        await db.refresh(publisher)
        return publisher

    async def delete_publisher(self, db: AsyncSession, publisher_id: int) -> bool:
        publisher = await self.get_by_id(db, publisher_id)
        if not publisher:
            return False
        # Материалы не удаляем — открепляем (FK nullable), как и при удалении
        # выпуска: издатель уходит, публикации остаются.
        await db.execute(
            Article.__table__.update()
            .where(Article.publisher_id == publisher_id)
            .values(publisher_id=None)
        )
        await db.delete(publisher)
        await db.commit()
        return True

    async def count_publications(self, db: AsyncSession, publisher_id: int) -> int:
        res = await db.execute(
            select(func.count(Article.id)).where(Article.publisher_id == publisher_id)
        )
        return int(res.scalar() or 0)

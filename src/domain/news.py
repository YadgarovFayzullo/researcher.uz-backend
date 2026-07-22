"""Домен новостей платформы.

Публичная видимость определяется одним предикатом (`_published_predicate`):
status='published' И published_at не позже часов БД. Черновик и отложенная
новость публично неотличимы от несуществующих — get_published_by_slug вернёт
None, роутер отдаст 404.
"""
import datetime
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from slugify import slugify

from src.infrastructure.persistence.models import NewsPost
from src.schemas.news import NewsCreate, NewsPublic, NewsUpdate


class NewsDomain:
    def generate_slug(self, text: str) -> str:
        return slugify(text)

    @staticmethod
    def _published_predicate():
        # func.now() — часы БД, а не приложения: исключает расхождение таймзон
        # между app-сервером и Postgres. Будущий published_at = отложенная
        # публикация, скрыт до наступления срока.
        return (
            (NewsPost.status == "published")
            & NewsPost.published_at.isnot(None)
            & (NewsPost.published_at <= func.now())
        )

    async def list_news(
        self,
        db: AsyncSession,
        *,
        lang: str | None = None,
        status: str | None = None,
        include_unpublished: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Срез новостей + общее число подходящих строк (для пагинации)."""
        stmt = select(NewsPost)
        if include_unpublished:
            # Админский путь: все статусы, опциональный фильтр.
            if status is not None:
                stmt = stmt.where(NewsPost.status == status)
        else:
            stmt = stmt.where(self._published_predicate())
        if lang is not None:
            stmt = stmt.where(NewsPost.lang == lang)

        total = (
            await db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()

        if include_unpublished:
            # У черновиков published_at пуст — админ-список сортируем по created_at.
            stmt = stmt.order_by(NewsPost.created_at.desc(), NewsPost.id.desc())
        else:
            stmt = stmt.order_by(
                NewsPost.published_at.desc().nullslast(), NewsPost.id.desc()
            )
        stmt = stmt.limit(limit).offset(offset)

        rows = (await db.execute(stmt)).scalars().all()
        # Через схему, а не обходом колонок: колонка "metadata" в БД доступна
        # только как атрибут `meta`, схема знает про алиас (см. domain/journal.py).
        items = [NewsPublic.model_validate(row).model_dump() for row in rows]
        return items, int(total)

    async def get_published_by_slug(self, db: AsyncSession, slug: str) -> NewsPost | None:
        result = await db.execute(
            select(NewsPost).where(NewsPost.slug == slug, self._published_predicate())
        )
        return result.scalars().first()

    async def get_by_slug(self, db: AsyncSession, slug: str) -> NewsPost | None:
        result = await db.execute(select(NewsPost).where(NewsPost.slug == slug))
        return result.scalars().first()

    async def get_by_id(self, db: AsyncSession, news_id: int) -> NewsPost | None:
        result = await db.execute(select(NewsPost).where(NewsPost.id == news_id))
        return result.scalars().first()

    async def create_news(
        self, db: AsyncSession, news_in: NewsCreate, *, admin_id
    ) -> NewsPost:
        """Создать новость; admin_id берётся из аутентифицированного owner-а."""
        data = news_in.model_dump(exclude_unset=True, by_alias=False)
        slug = data.pop("slug", None) or self.generate_slug(news_in.title)
        if await self.get_by_slug(db, slug):
            slug = f"{slug}-{str(uuid.uuid4())[:6]}"
        # «Опубликовать сейчас» без явной даты — ставим момент создания.
        if data.get("status") == "published" and data.get("published_at") is None:
            data["published_at"] = datetime.datetime.now(datetime.timezone.utc)

        post = NewsPost(**data, slug=slug, admin_id=admin_id)
        db.add(post)
        await db.commit()
        await db.refresh(post)
        return post

    async def update_news(
        self, db: AsyncSession, news_id: int, news_in: NewsUpdate
    ) -> NewsPost | None:
        """Обновить новость.

        Slug при смене заголовка не пересчитывается — он в публичных и
        канонических URL (/uz/news/<slug>), см. update_journal.
        """
        post = await self.get_by_id(db, news_id)
        if not post:
            return None

        data = news_in.model_dump(exclude_unset=True, by_alias=False)
        # Первая публикация без явной даты — фиксируем момент.
        if (
            data.get("status") == "published"
            and post.published_at is None
            and data.get("published_at") is None
        ):
            data["published_at"] = datetime.datetime.now(datetime.timezone.utc)
        for field, value in data.items():
            setattr(post, field, value)

        await db.commit()
        await db.refresh(post)
        return post

    async def delete_news(self, db: AsyncSession, news_id: int) -> bool:
        post = await self.get_by_id(db, news_id)
        if not post:
            return False
        await db.delete(post)
        await db.commit()
        return True

"""Схемы новостей платформы (`news`).

Тело новости — HTML из Tiptap; фронт санитайзит его при рендере
(NewsBody.tsx), поэтому здесь оно хранится/отдаётся как есть.
"""
import datetime
from typing import Any
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

# Порядок алиасов важен: у модели SQLAlchemy атрибут `metadata` занят объектом
# MetaData, поэтому при чтении из ORM имя колонки `meta` проверяется первым.
_META_IN = AliasChoices("meta", "metadata")


class IDMixin(BaseModel):
    id: int


class NewsBase(BaseModel):
    title: str = ""
    excerpt: str | None = None  # тизер для карточек + meta description
    body_html: str = ""
    cover_image: str | None = None
    lang: str = "ru"  # ru | uz | en (CHECK в БД)
    status: str = "draft"  # draft | published (CHECK в БД)
    # NULL у черновика; будущее значение = отложенная публикация.
    published_at: datetime.datetime | None = None
    meta: dict[str, Any] | None = Field(
        None, validation_alias=_META_IN, serialization_alias="metadata"
    )


class NewsCreate(NewsBase):
    model_config = ConfigDict(populate_by_name=True)
    # Не прислали — домен выведет из title.
    slug: str | None = None


class NewsUpdate(BaseModel):
    """Применяется через `exclude_unset` — не присланное поле не трогается."""

    model_config = ConfigDict(populate_by_name=True)

    title: str | None = None
    slug: str | None = None
    excerpt: str | None = None
    body_html: str | None = None
    cover_image: str | None = None
    lang: str | None = None
    status: str | None = None
    published_at: datetime.datetime | None = None
    meta: dict[str, Any] | None = Field(
        None, validation_alias=_META_IN, serialization_alias="metadata"
    )


class NewsPublic(NewsBase, IDMixin):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    slug: str
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None
    admin_id: UUID | None = None


class NewsListResponse(BaseModel):
    """Срез списка + общее число — для серверной пагинации."""

    items: list[NewsPublic]
    total: int

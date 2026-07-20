"""Схемы под-ресурсов контента: секции конференций, авторы статьи,
пристатейные ссылки, сохранённые статьи (библиотека).

Формы ответов повторяют то, что фронт сегодня получает от Supabase-эмбедов
(`article_authors -> articles -> issues -> journals` и т.п.), чтобы переключение
не требовало переписывать рендер.
"""
from __future__ import annotations

import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


# --------------------------------------------------------- conference_sections
class SectionBase(BaseModel):
    title: str
    description: str | None = None
    position: int = 0


class SectionCreate(SectionBase):
    issue_id: int


class SectionUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    position: int | None = None


class SectionPublic(SectionBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    issue_id: int
    created_at: datetime.datetime | None = None


class SectionReorder(BaseModel):
    """Новый порядок секций тома: список id в нужной последовательности.

    Фронт сегодня меняет порядок двумя UPDATE'ами (обмен position соседей) — это
    две несогласованные записи; здесь порядок применяется одной транзакцией.
    """

    issue_id: int
    section_ids: list[int]


# ------------------------------------------------------------- article_authors
class AuthorIn(BaseModel):
    author_name: str
    author_order: int
    orcid: str | None = None
    profile_id: UUID | None = None


class AuthorPublic(AuthorIn):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    article_id: int
    is_verified: bool | None = None


class AuthorsReplace(BaseModel):
    authors: list[AuthorIn]


# ---------------------------------------------------------- article_references
class ReferenceIn(BaseModel):
    raw: str | None = None
    cited_doi: str | None = None
    cited_article_id: int | None = None
    position: int | None = None


class ReferencePublic(ReferenceIn):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    article_id: int
    # Заголовок процитированной статьи, если она есть на платформе
    # (Supabase-эмбед `cited:articles(title)`).
    cited_title: str | None = None


class ReferencesReplace(BaseModel):
    references: list[ReferenceIn]


# -------------------------------------------------------------- saved_articles
class SavedState(BaseModel):
    saved: bool

"""Схемы журналов и серий конференций (`journals`).

Одна таблица на оба вида: `type` = 'journal' | 'conference_series'. У серии в
`metadata` лежит оргкомитет, а по `type` фронт решает, вести ли на /journal или
на /conference — поэтому оба поля обязаны быть в публичном ответе.
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


class JournalBase(BaseModel):
    name: str = ""
    site_link: str | None = None
    issn: str | None = None
    printed_issn: str | None = None
    vak: str | None = None  # В базе это text
    google_scholar: str | None = None
    cover_image: str | None = None
    description: str | None = None
    theme: str | None = None
    publisher: str | None = None
    logo: str | None = None
    subject_codes: list[str] = Field(default_factory=list)
    type: str | None = None
    meta: dict[str, Any] | None = Field(
        None, validation_alias=_META_IN, serialization_alias="metadata"
    )


class JournalCreate(JournalBase):
    model_config = ConfigDict(populate_by_name=True)
    slug: str


class JournalUpdate(BaseModel):
    """Применяется через `exclude_unset` — не присланное поле не трогается."""

    model_config = ConfigDict(populate_by_name=True)

    name: str | None = None
    site_link: str | None = None
    issn: str | None = None
    printed_issn: str | None = None
    vak: str | None = None
    google_scholar: str | None = None
    cover_image: str | None = None
    description: str | None = None
    theme: str | None = None
    publisher: str | None = None
    logo: str | None = None
    subject_codes: list[str] | None = None
    type: str | None = None
    meta: dict[str, Any] | None = Field(
        None, validation_alias=_META_IN, serialization_alias="metadata"
    )


class JournalPublic(JournalBase, IDMixin):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    slug: str
    created_at: datetime.datetime | None = None
    admin_id: UUID | None = None
    # Заполняется только при ?with_issue_counts=true, иначе 0.
    issues_count: int = 0


class JournalAdmin(JournalPublic):
    pass

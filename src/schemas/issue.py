"""Схемы выпусков (`issues`).

Выпуск журнала и том материалов конференции — одна таблица (см. conferences.sql):
для журнала значимы year/volume/issue, для конференции — title/date_start/date_end/
location/isbn.

`metadata` в JSON ↔ атрибут `meta` модели: у SQLAlchemy `Base.metadata` занято
объектом MetaData, поэтому колонка называется `meta`, а наружу отдаётся под
исходным именем через alias.
"""
from __future__ import annotations

import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

# Порядок важен: при чтении из ORM (`from_attributes`) Pydantic идёт по алиасам
# слева направо, а атрибут `metadata` у модели SQLAlchemy занят объектом
# MetaData — поэтому имя колонки `meta` должно проверяться первым. Из JSON
# клиенты присылают `metadata`, его принимаем вторым.
_META_IN = AliasChoices("meta", "metadata")


class IssueBase(BaseModel):
    journal_id: int | None = None
    year: int | None = None
    volume: str | None = None
    issue: str | None = None
    full_pdf: str | None = None
    title: str | None = None
    date_start: datetime.date | None = None
    date_end: datetime.date | None = None
    location: str | None = None
    isbn: str | None = None
    cover_image: str | None = None
    meta: dict[str, Any] = Field(
        default_factory=dict, validation_alias=_META_IN, serialization_alias="metadata"
    )


class IssueCreate(IssueBase):
    model_config = ConfigDict(populate_by_name=True)
    journal_id: int


class IssueUpdate(BaseModel):
    """Все поля опциональны; `exclude_unset` в домене отличает «не передано»
    от «передано null» (последнее очищает поле)."""

    model_config = ConfigDict(populate_by_name=True)

    year: int | None = None
    volume: str | None = None
    issue: str | None = None
    full_pdf: str | None = None
    title: str | None = None
    date_start: datetime.date | None = None
    date_end: datetime.date | None = None
    location: str | None = None
    isbn: str | None = None
    cover_image: str | None = None
    meta: dict[str, Any] | None = Field(
        None, validation_alias=_META_IN, serialization_alias="metadata"
    )


class IssuePublic(IssueBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    created_at: datetime.datetime | None = None
    # Заполняется только в списке (JournalInfo показывает число статей в выпуске).
    article_count: int | None = None

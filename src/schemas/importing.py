"""Схемы импорта архивов (`/import`)."""
from __future__ import annotations

import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ImportJobCreate(BaseModel):
    journal_id: int
    source_type: str = "table"
    source_ref: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class ImportJobPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    journal_id: int
    source_type: str
    source_ref: str | None = None
    status: str
    totals: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime.datetime
    finished_at: datetime.datetime | None = None


class ImportItemPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    source_key: str
    parsed: dict[str, Any] = Field(default_factory=dict)
    issue_key: str | None = None
    status: str
    problems: list[dict[str, Any]] = Field(default_factory=list)
    article_id: int | None = None
    pdf_source: str | None = None
    pdf_url: str | None = None


class ImportItemsPage(BaseModel):
    items: list[ImportItemPublic]
    total: int


class ImportItemUpdate(BaseModel):
    """Правка кандидата до применения: поля статьи и/или статус."""

    parsed: dict[str, Any] | None = None
    status: str | None = None


class ImportApplyRequest(BaseModel):
    # Пусто — применить всё готовое; список — только выбранные строки.
    item_ids: list[int] | None = None


class ImportParseResult(BaseModel):
    job: ImportJobPublic
    parsed: int
    skipped_in_file: int
    unknown_columns: list[str] = Field(default_factory=list)


class ImportPdfResult(BaseModel):
    matched: bool
    item_id: int | None = None
    url: str


class OaiDiscoverRequest(BaseModel):
    """Разведка старого сайта: адрес вводит клиент, журнал — для проверки прав."""

    journal_id: int
    site_url: str


class OaiSetPublic(BaseModel):
    spec: str
    name: str


class OaiDiscoverResult(BaseModel):
    base_url: str
    repository_name: str
    sets: list[OaiSetPublic] = Field(default_factory=list)
    # Журнал, угаданный из вставленной ссылки (если он есть среди sets).
    suggested_set: str | None = None


class OaiParseRequest(BaseModel):
    """Что именно забирать: журнал (сет) и, по желанию, диапазон дат.

    `resume` продолжает прерванный обход с сохранённой закладки: архив бывает
    больше потолка задачи (2000 записей), и хвост дозабирается той же задачей.
    """

    set_spec: str | None = None
    date_from: str | None = None   # YYYY-MM-DD
    date_until: str | None = None
    resume: bool = False

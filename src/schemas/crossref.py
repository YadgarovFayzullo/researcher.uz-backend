"""Схемы регистрации DOI в Crossref (`/crossref`)."""
from __future__ import annotations

import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CrossrefDepositPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    article_id: int
    environment: str
    doi: str
    batch_id: str | None = None
    status: str
    attempts: int
    error: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime.datetime
    submitted_at: datetime.datetime | None = None
    checked_at: datetime.datetime | None = None
    registered_at: datetime.datetime | None = None


class CrossrefContributor(BaseModel):
    given_name: str
    surname: str
    orcid: str = ""


class CrossrefPreview(BaseModel):
    """Что именно уедет в Crossref — до того, как уедет.

    Показывать это обязательно: разбор ФИО эвристический, а депозит необратим.
    Редактор смотрит на `contributors` и правит `author_name` до отправки, а не
    ищет потом, почему в мировом индексе у автора имя и фамилия поменялись
    местами.
    """

    article_id: int
    doi: str
    environment: str
    resource_url: str
    journal_name: str | None = None
    issn: str | None = None
    printed_issn: str | None = None
    volume: str | None = None
    issue: str | None = None
    title: str | None = None
    publication_date: datetime.date
    first_page: str = ""
    last_page: str = ""
    contributors: list[CrossrefContributor] = Field(default_factory=list)
    citations_count: int = 0
    citations_with_doi: int = 0
    # Пусто = депозит можно отправлять. Иначе — чего не хватает.
    problems: list[str] = Field(default_factory=list)
    deposit: CrossrefDepositPublic | None = None
    xml: str | None = None

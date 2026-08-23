"""Схемы проверки на заимствования (`/plagiarism`)."""
from __future__ import annotations

import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class PlagiarismCheckPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    article_id: int | None = None
    journal_id: int | None = None
    title: str | None = None
    status: str
    # Процент заимствований. Numeric из БД приходит Decimal — отдаём числом,
    # иначе клиент получит строку и будет сравнивать её как текст.
    score: float | None = None
    words_count: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    # Текст документа — отчёт рисует его целиком с подсветкой заимствований.
    content: str | None = None
    created_at: datetime.datetime
    finished_at: datetime.datetime | None = None

    @field_serializer("score")
    def _round_score(self, value: float | None) -> float | None:
        return None if value is None else round(float(value), 2)


class PlagiarismMatchPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_article_id: int | None = None
    source_url: str | None = None
    source_title: str | None = None
    matched_shingles: int
    score: float
    fragments: list[dict[str, Any]] = Field(default_factory=list)
    # Интервалы [начало, конец) в символах `check.content`.
    spans: list[list[int]] = Field(default_factory=list)

    @field_serializer("score")
    def _round_score(self, value: float) -> float:
        return round(float(value), 2)


class PlagiarismReport(BaseModel):
    """Проверка вместе с источниками — то, что рисует экран отчёта."""

    check: PlagiarismCheckPublic
    matches: list[PlagiarismMatchPublic] = Field(default_factory=list)


class PlagiarismCoverage(BaseModel):
    """Готовность базы к проверкам: сравнивать можно только с тем, что проиндексировано."""

    articles: int
    indexed: int
    scans: int

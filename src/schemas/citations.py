"""Схемы цитирований (Фаза 5)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ArticleIdsRequest(BaseModel):
    article_ids: list[int] = []


class DoisRequest(BaseModel):
    dois: list[str] = []


class CitationCount(BaseModel):
    article_id: int
    internal_count: int
    external_count: int
    cited_by: int


class CitingArticle(BaseModel):
    id: int
    title: str | None = None
    slug: str | None = None
    authors: str | None = None
    publication_year: int | None = None
    created_at: datetime | None = None


class DoiMatch(BaseModel):
    norm_doi: str
    id: int
    title: str | None = None
    slug: str | None = None


class ExternalCitationRow(BaseModel):
    article_id: int
    doi: str | None = None
    cited_by_count: int = 0
    counts_by_year: list = []
    source: str | None = None


class UpsertExternalRequest(BaseModel):
    rows: list[ExternalCitationRow] = []


class RefreshRequest(BaseModel):
    limit: int = 500
    offset: int = 0

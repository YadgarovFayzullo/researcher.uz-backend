"""Схемы кабинета исследователя (Фаза 5)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class UpdateMyProfileRequest(BaseModel):
    """NULL (поле отсутствует) = не трогать; '' = очистить."""
    full_name: str | None = None
    workplace: str | None = None
    country: str | None = None
    education: str | None = None
    bio: str | None = None
    avatar_url: str | None = None


class DoisRequest(BaseModel):
    dois: list[str] = []


class OrcidWork(BaseModel):
    put_code: str
    title: str | None = None
    work_type: str | None = None
    year: int | str | None = None
    doi: str | None = None
    url: str | None = None
    container: str | None = None


class ImportWorksRequest(BaseModel):
    works: list[OrcidWork] = []


class ResearcherProfile(BaseModel):
    full_name: str | None = None
    orcid: str | None = None
    avatar_url: str | None = None
    workplace: str | None = None
    country: str | None = None
    bio: str | None = None
    education: str | None = None


class ResearcherWorkPublic(BaseModel):
    put_code: str
    title: str | None = None
    work_type: str | None = None
    year: int | None = None
    doi: str | None = None
    url: str | None = None
    container: str | None = None

    model_config = ConfigDict(from_attributes=True)


class ResearcherPageResponse(BaseModel):
    profile: ResearcherProfile
    works: list[ResearcherWorkPublic] = []

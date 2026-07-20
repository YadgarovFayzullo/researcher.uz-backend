"""Схемы издателей (`publishers`) — см. supabase/publishers.sql."""
from __future__ import annotations

import datetime
import re

from pydantic import BaseModel, ConfigDict, field_validator
from uuid import UUID

# Тот же инвариант, что и CHECK-констрейнт publishers_slug_check в БД —
# ловим его до похода в Postgres, чтобы отдать 422, а не 500.
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class PublisherBase(BaseModel):
    name: str
    description: str | None = None
    logo: str | None = None
    website: str | None = None


class PublisherCreate(PublisherBase):
    slug: str

    @field_validator("slug")
    @classmethod
    def check_slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("slug: только строчные латинские буквы, цифры и дефис")
        return v


class PublisherUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    logo: str | None = None
    website: str | None = None
    # admin_id меняет только owner (проверяется в эндпоинте).
    admin_id: UUID | None = None


class PublisherPublic(PublisherBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    admin_id: UUID | None = None
    created_at: datetime.datetime | None = None

"""Схемы онлайн-сессий конференций (`/conference-sessions`)."""
from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SessionStatus = Literal["scheduled", "cancelled"]
MeetRole = Literal["moderator", "speaker", "participant"]


class ConferenceSessionCreate(BaseModel):
    issue_id: int
    section_id: int | None = None
    title: str = Field(min_length=1, max_length=200)
    starts_at: datetime.datetime
    ends_at: datetime.datetime | None = None


class ConferenceSessionUpdate(BaseModel):
    section_id: int | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    starts_at: datetime.datetime | None = None
    ends_at: datetime.datetime | None = None
    status: SessionStatus | None = None


class ConferenceSessionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    issue_id: int
    section_id: int | None = None
    title: str
    starts_at: datetime.datetime
    ends_at: datetime.datetime | None = None
    room: str
    status: SessionStatus
    created_at: datetime.datetime | None = None
    recording_url: str | None = None
    recording_size: int | None = None
    recording_uploaded_at: datetime.datetime | None = None


class JoinResponse(BaseModel):
    """Адрес входа в комнату. Токен внутри одноразового смысла не имеет, но живёт
    недолго (MEET_TOKEN_EXPIRE_HOURS) и привязан к одной комнате."""

    url: str
    role: MeetRole
    expires_at: datetime.datetime


class RecordingUploadRequest(BaseModel):
    """Организатор просит адрес для прямой загрузки файла записи в R2."""

    filename: str = Field(min_length=1, max_length=200)
    content_type: str = Field(default="video/webm", max_length=100)
    size: int = Field(gt=0, le=5 * 1024 * 1024 * 1024)


class RecordingUploadResponse(BaseModel):
    upload_url: str
    key: str
    public_url: str
    expires_in: int


class RecordingAttach(BaseModel):
    """После PUT в R2 фронт сообщает ключ; сервер сверяет, что объект есть."""

    key: str = Field(min_length=1, max_length=300)

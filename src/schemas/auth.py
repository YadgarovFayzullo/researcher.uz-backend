from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    password: str
    full_name: str | None = None
    # Токен виджета Cloudflare Turnstile. Обязателен, только если на бэкенде
    # задан TURNSTILE_SECRET_KEY (иначе поле игнорируется).
    turnstile_token: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str | None = None
    avatar_url: str | None = None
    role: str | None = None
    username: str | None = None
    orcid_id: str | None = None


class UserMe(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str | None = None
    email_confirmed_at: datetime | None = None
    created_at: datetime | None = None
    profile: ProfileOut | None = None

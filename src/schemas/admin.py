from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class ProfileRow(BaseModel):
    id: uuid.UUID
    full_name: str | None = None
    email: str | None = None
    role: str | None = None
    created_at: datetime | None = None


class JournalAdminRow(BaseModel):
    journal_id: int
    user_id: uuid.UUID
    full_name: str | None = None
    email: str | None = None


class EditorOption(BaseModel):
    id: uuid.UUID
    full_name: str | None = None
    email: str | None = None
    role: str | None = None


class SetUserRoleRequest(BaseModel):
    target_user: uuid.UUID
    new_role: str  # 'admin' | 'authenticated'


class SetJournalAdminRequest(BaseModel):
    target_journal: int
    target_user: uuid.UUID
    attach: bool

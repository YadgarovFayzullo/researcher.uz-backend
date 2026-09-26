"""Онлайн-сессии конференций (`/conference-sessions`).

Расписание читается публично (страница сборника показывает «идёт сейчас»),
запись — как у секций: owner ИЛИ journal_admin серии (`can_write_issue`).
Вход в комнату (`/join`) требует сессии на платформе; роль в комнате решает
`src/domain/meet.py`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_profile
from src.domain.authz import can_write_issue
from src.domain.issue import IssueDomain
from fastapi.concurrency import run_in_threadpool

from src.domain.meet import MeetDisabled, MeetDomain, SessionClosed
from src.infrastructure.storage import StorageNotConfigured, public_url, storage
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import ConferenceSession, Profile
from src.schemas.meet import (
    ConferenceSessionCreate,
    ConferenceSessionPublic,
    ConferenceSessionUpdate,
    JoinResponse,
    RecordingAttach,
    RecordingUploadRequest,
    RecordingUploadResponse,
)

router = APIRouter()
domain = MeetDomain()
issues = IssueDomain()


async def _guard_issue(db: AsyncSession, profile: Profile, issue_id: int) -> None:
    issue = await issues.get_issue(db, issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    if not await can_write_issue(
        db, role=profile.role, user_id=profile.id, journal_id=issue.journal_id
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Not allowed to write this issue"
        )


async def _get_or_404(db: AsyncSession, session_id: int) -> ConferenceSession:
    session = await domain.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.get("/", response_model=list[ConferenceSessionPublic])
async def list_sessions(
    issue_id: int = Query(...), db: AsyncSession = Depends(get_db)
):
    return await domain.list_sessions(db, issue_id)


@router.post("/", response_model=ConferenceSessionPublic, status_code=201)
async def create_session(
    data: ConferenceSessionCreate,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    await _guard_issue(db, profile, data.issue_id)
    try:
        return await domain.create_session(db, data, created_by=profile.id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.patch("/{session_id}", response_model=ConferenceSessionPublic)
async def update_session(
    session_id: int,
    data: ConferenceSessionUpdate,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    session = await _get_or_404(db, session_id)
    await _guard_issue(db, profile, session.issue_id)
    try:
        return await domain.update_session(db, session, data)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    session = await _get_or_404(db, session_id)
    await _guard_issue(db, profile, session.issue_id)
    await domain.delete_session(db, session)


@router.post("/{session_id}/join", response_model=JoinResponse)
async def join_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    session = await _get_or_404(db, session_id)
    try:
        url, role, exp = await domain.join(db, profile, session)
    except SessionClosed as e:
        # 409, а не 403: право есть, просто не время. Фронт показывает, когда откроется.
        raise HTTPException(
            status_code=409, detail={"code": "session_closed", "reason": e.reason}
        )
    except MeetDisabled:
        raise HTTPException(status_code=503, detail="Meet is not configured")
    return JoinResponse(url=url, role=role, expires_at=exp)


# ------------------------------------------------------------------ запись
# Файл идёт из браузера организатора прямо в R2 по подписанному URL: через
# API запись на гигабайты не прогнать. Права — как на правку сессии.

UPLOAD_URL_TTL = 6 * 3600  # загрузка 1–2 ГБ по медленному каналу — часы


@router.post("/{session_id}/recording/upload-url", response_model=RecordingUploadResponse)
async def recording_upload_url(
    session_id: int,
    data: RecordingUploadRequest,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    session = await _get_or_404(db, session_id)
    await _guard_issue(db, profile, session.issue_id)
    if not data.content_type.startswith(("video/", "audio/")):
        raise HTTPException(status_code=422, detail="Only video or audio files")
    key = domain.recording_key(session, data.filename)
    try:
        url = await run_in_threadpool(
            storage.presigned_put_url, key, data.content_type, UPLOAD_URL_TTL
        )
    except StorageNotConfigured:
        raise HTTPException(status_code=503, detail="Storage not configured")
    return RecordingUploadResponse(
        upload_url=url, key=key, public_url=public_url(key) or key, expires_in=UPLOAD_URL_TTL
    )


@router.put("/{session_id}/recording", response_model=ConferenceSessionPublic)
async def attach_recording(
    session_id: int,
    data: RecordingAttach,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    session = await _get_or_404(db, session_id)
    await _guard_issue(db, profile, session.issue_id)
    try:
        return await domain.attach_recording(db, session, data.key)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except StorageNotConfigured:
        raise HTTPException(status_code=503, detail="Storage not configured")


@router.delete("/{session_id}/recording", response_model=ConferenceSessionPublic)
async def remove_recording(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    session = await _get_or_404(db, session_id)
    await _guard_issue(db, profile, session.issue_id)
    return await domain.remove_recording(db, session)

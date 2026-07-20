"""Выпуски (`/issues`).

Чтение публично (RLS: `using (true)`). Запись — owner ИЛИ journal_admin
родительского журнала (`can_write_issue`, зеркалит rls_content.sql).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_profile
from src.domain.authz import can_write_issue
from src.domain.issue import IssueDomain
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Profile
from src.schemas.issue import IssueCreate, IssuePublic, IssueUpdate

router = APIRouter()
domain = IssueDomain()


def _forbidden() -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed to write this issue")


async def _guard(db: AsyncSession, profile: Profile, journal_id) -> None:
    if not await can_write_issue(
        db, role=profile.role, user_id=profile.id, journal_id=journal_id
    ):
        raise _forbidden()


@router.get("/", response_model=list[IssuePublic])
async def list_issues(
    journal_id: int | None = Query(None),
    year: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    return await domain.list_issues(db, journal_id=journal_id, year=year)


@router.get("/years", response_model=list[int])
async def list_years(journal_id: int = Query(...), db: AsyncSession = Depends(get_db)):
    """Годы с выпусками у журнала (фильтр в DOIlist).

    Объявлен до `/{issue_id}`, иначе "years" уедет в int-параметр пути → 422.
    """
    return await domain.list_years(db, journal_id)


@router.get("/{issue_id}", response_model=IssuePublic)
async def get_issue(issue_id: int, db: AsyncSession = Depends(get_db)):
    issue = await domain.get_issue(db, issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    return issue


@router.post("/", response_model=IssuePublic, status_code=201)
async def create_issue(
    issue_in: IssueCreate,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    await _guard(db, profile, issue_in.journal_id)
    return await domain.create_issue(db, issue_in)


@router.patch("/{issue_id}", response_model=IssuePublic)
async def update_issue(
    issue_id: int,
    issue_in: IssueUpdate,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    existing = await domain.get_issue(db, issue_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Issue not found")
    # Право на существующую строку: IssueUpdate не меняет journal_id,
    # поэтому `with check` совпадает с `using`.
    await _guard(db, profile, existing.journal_id)
    return await domain.update_issue(db, issue_id, issue_in)


@router.delete("/{issue_id}")
async def delete_issue(
    issue_id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    existing = await domain.get_issue(db, issue_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Issue not found")
    await _guard(db, profile, existing.journal_id)
    await domain.delete_issue(db, issue_id)
    return {"status": "deleted"}

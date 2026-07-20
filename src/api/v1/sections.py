"""Секции материалов конференции (`/conference-sections`).

Секция принадлежит тому (`issues`), поэтому право на запись — то же, что на
родительский том: owner ИЛИ journal_admin серии (`can_write_issue`).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_profile
from src.domain.authz import can_write_issue
from src.domain.content import SectionDomain
from src.domain.issue import IssueDomain
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Profile
from src.schemas.content import (
    SectionCreate,
    SectionPublic,
    SectionReorder,
    SectionUpdate,
)

router = APIRouter()
domain = SectionDomain()
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


@router.get("/", response_model=list[SectionPublic])
async def list_sections(issue_id: int = Query(...), db: AsyncSession = Depends(get_db)):
    return await domain.list_sections(db, issue_id)


@router.post("/", response_model=SectionPublic, status_code=201)
async def create_section(
    section_in: SectionCreate,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    await _guard_issue(db, profile, section_in.issue_id)
    return await domain.create_section(db, section_in)


@router.post("/reorder", response_model=list[SectionPublic])
async def reorder_sections(
    payload: SectionReorder,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    await _guard_issue(db, profile, payload.issue_id)
    return await domain.reorder(db, payload.issue_id, payload.section_ids)


@router.patch("/{section_id}", response_model=SectionPublic)
async def update_section(
    section_id: int,
    section_in: SectionUpdate,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    existing = await domain.get_section(db, section_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Section not found")
    await _guard_issue(db, profile, existing.issue_id)
    return await domain.update_section(db, section_id, section_in)


@router.delete("/{section_id}")
async def delete_section(
    section_id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    existing = await domain.get_section(db, section_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Section not found")
    await _guard_issue(db, profile, existing.issue_id)
    await domain.delete_section(db, section_id)
    return {"status": "deleted"}

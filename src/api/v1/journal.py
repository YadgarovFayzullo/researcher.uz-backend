from fastapi import Depends, APIRouter, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.deps import require_owner
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Profile
from src.domain.journal import JournalDomain
from src.schemas.journal import JournalPublic, JournalCreate, JournalUpdate

router = APIRouter()
domain = JournalDomain()

# Объявлен ДО /{slug}: иначе "by-id" уедет в slug и журнал не найдётся.
@router.get("/by-id/{journal_id}", response_model=JournalPublic)
async def get_journal_by_id(journal_id: int, db: AsyncSession = Depends(get_db)):
    journal = await domain.get_journal_by_id(db, journal_id)
    if not journal:
        raise HTTPException(status_code=404, detail="Journal not found")
    return journal


@router.get("/{slug}", response_model=JournalPublic)
async def get_journal(slug: str, db: AsyncSession = Depends(get_db)):
    journal = await domain.get_journal_by_slug(db, slug)
    if not journal:
        raise HTTPException(status_code=404, detail="Journal not found")
    return journal


# journals — только owner (rls_content.sql: journals writes owner-only).
@router.post("/", response_model=JournalPublic, status_code=201)
async def create_journal(
    journal_in: JournalCreate,
    db: AsyncSession = Depends(get_db),
    _owner: Profile = Depends(require_owner),
):
    return await domain.create_journal(db, journal_in)


@router.patch("/{journal_id}", response_model=JournalPublic)
async def update_journal(
    journal_id: int,
    journal_in: JournalUpdate,
    db: AsyncSession = Depends(get_db),
    _owner: Profile = Depends(require_owner),
):
    journal = await domain.update_journal(db, journal_id, journal_in)
    if not journal:
        raise HTTPException(status_code=404, detail="Journal not found")
    return journal


@router.delete("/{journal_id}")
async def delete_journal(
    journal_id: int,
    db: AsyncSession = Depends(get_db),
    _owner: Profile = Depends(require_owner),
):
    success = await domain.delete_journal(db, journal_id)
    if not success:
        raise HTTPException(status_code=404, detail="Journal not found")
    return {"status": "deleted"}

from fastapi import Depends, APIRouter
from sqlalchemy.ext.asyncio import AsyncSession
from src.infrastructure.persistence.db import get_db
from src.domain.journal import JournalDomain
from src.schemas.journal import JournalPublic

router = APIRouter()
domain = JournalDomain()

@router.get("/", response_model=list[JournalPublic])
async def list_journals(db: AsyncSession = Depends(get_db)):
    """List all journals."""
    return await domain.list_journals(db)

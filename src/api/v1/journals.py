from fastapi import Depends, APIRouter, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.infrastructure.persistence.db import get_db
from src.domain.journal import JournalDomain
from src.schemas.journal import JournalPublic

router = APIRouter()
domain = JournalDomain()

@router.get("/", response_model=list[JournalPublic])
async def list_journals(
    type: str | None = Query(
        None,
        description="'journal' или 'conference_series' — каталог конференций берёт только вторые",
    ),
    with_issue_counts: bool = Query(
        False, description="добавить issues_count (число выпусков/томов)"
    ),
    include_demo: bool = Query(
        False,
        description="показать и демо-журналы (metadata.demo) — нужно owner-консоли",
    ),
    db: AsyncSession = Depends(get_db),
):
    """Список журналов, новые сверху."""
    return await domain.list_journals(
        db,
        type_=type,
        with_issue_counts=with_issue_counts,
        include_demo=include_demo,
    )

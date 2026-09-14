"""Корзина удалённых статей (src/domain/article_trash.py). Owner-only.

Удаляют статьи и редакторы журналов, но возвращать их — решение владельца:
восстановление поднимает статью на сайт с прежним адресом и DOI.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import require_owner
from src.domain import article_trash
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Profile

router = APIRouter()


@router.get("/")
async def list_trash(
    q: str | None = Query(None, max_length=200),
    journal_id: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _owner: Profile = Depends(require_owner),
):
    return await article_trash.list_trash(
        db, q=q, journal_id=journal_id, limit=limit, offset=offset
    )


@router.post("/{trash_id}/restore")
async def restore_article(
    trash_id: int,
    db: AsyncSession = Depends(get_db),
    _owner: Profile = Depends(require_owner),
):
    try:
        result = await article_trash.restore(db, trash_id)
    except article_trash.TrashNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Записи в корзине нет")
    except article_trash.TrashConflict as err:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    return {
        "status": "restored",
        **result,
        "revalidate_slugs": [result["slug"]] if result["slug"] else [],
    }

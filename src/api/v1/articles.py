"""Списки статей (`/articles`) — публичное чтение с фильтрами и пагинацией.

Одиночная статья по slug и запись — в `article.py` (`/article`).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.article import ArticleDomain
from src.infrastructure.persistence.db import get_db
from src.schemas.article import ArticleListResponse, ArticlePublic

router = APIRouter()
domain = ArticleDomain()

# Потолок страницы: без него один запрос с limit=100000 выгружает всю базу.
MAX_LIMIT = 200


@router.get("/", response_model=ArticleListResponse)
async def list_articles(
    db: AsyncSession = Depends(get_db),
    issue_id: list[int] | None = Query(None),
    journal_id: list[int] | None = Query(
        None, description="Все статьи журнала(ов) через их выпуски"
    ),
    publisher_id: int | None = Query(None),
    admin_id: str | None = Query(None),
    section_id: int | None = Query(None),
    publication_type: list[str] | None = Query(None),
    field_of_science: str | None = Query(None),
    published: bool | None = Query(None),
    has_doi: bool | None = Query(None),
    created_after: datetime | None = Query(
        None, description="только статьи, созданные не раньше этой даты (ISO)"
    ),
    has_issue: bool | None = Query(
        None, description="true — только статьи выпусков, false — только самостоятельные издания"
    ),
    order_by: str = Query("created_at"),
    descending: bool = Query(True),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    with_stats: bool = Query(False, description="Доклеить просмотры/скачивания"),
):
    items, total = await domain.list_articles(
        db,
        issue_id=issue_id,
        journal_id=journal_id,
        publisher_id=publisher_id,
        admin_id=admin_id,
        section_id=section_id,
        publication_type=publication_type,
        field_of_science=field_of_science,
        published=published,
        has_doi=has_doi,
        has_issue=has_issue,
        created_after=created_after,
        order_by=order_by,
        descending=descending,
        limit=limit,
        offset=offset,
        with_stats=with_stats,
    )
    return {"items": items, "total": total}


@router.get("/count")
async def count_articles(
    db: AsyncSession = Depends(get_db),
    issue_id: list[int] | None = Query(None),
    journal_id: list[int] | None = Query(None),
    publisher_id: int | None = Query(None),
    admin_id: str | None = Query(None),
    section_id: int | None = Query(None),
    publication_type: list[str] | None = Query(None),
    field_of_science: str | None = Query(None),
    published: bool | None = Query(None),
    has_doi: bool | None = Query(None),
    created_after: datetime | None = Query(
        None, description="только статьи, созданные не раньше этой даты (ISO)"
    ),
    has_issue: bool | None = Query(
        None, description="true — только статьи выпусков, false — только самостоятельные издания"
    ),
):
    """Только число — замена `select('*', {count:'exact', head:true})` фронта."""
    total = await domain.count_articles(
        db,
        issue_id=issue_id,
        journal_id=journal_id,
        publisher_id=publisher_id,
        admin_id=admin_id,
        section_id=section_id,
        publication_type=publication_type,
        field_of_science=field_of_science,
        published=published,
        has_doi=has_doi,
        has_issue=has_issue,
        created_after=created_after,
    )
    return {"count": total}


class LookupRequest(BaseModel):
    slugs: list[str] = []
    ids: list[int] = []


@router.post("/lookup")
async def lookup_articles(body: LookupRequest, db: AsyncSession = Depends(get_db)):
    """Батч {id, title, slug} по слугам/id — для матчинга списка литературы.

    POST, а не GET: список ссылок бывает длинным, в query-строку не влезет.
    """
    return await domain.lookup(db, slugs=body.slugs, ids=body.ids)


@router.get("/fields-of-science")
async def fields_of_science(db: AsyncSession = Depends(get_db)):
    """Фасет {field, count} для блока направлений науки."""
    return await domain.fields_of_science(db)


# Объявлен после литеральных путей выше — иначе "count" и "fields-of-science"
# уехали бы в int-параметр пути и вернули 422.
@router.get("/by-id/{article_id}", response_model=ArticlePublic)
async def get_article_by_id(article_id: int, db: AsyncSession = Depends(get_db)):
    article = await domain.get_article_by_id(db, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article

"""Новости платформы.

Один роутер на чтение и записи (осознанное отступление от пары
plural/singular a-la journals.py/journal.py: «news» неисчисляемое, второго
префикса не получится). Литеральные пути объявлены ДО /{slug}.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import require_owner
from src.domain.news import NewsDomain
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Profile
from src.schemas.news import NewsCreate, NewsListResponse, NewsPublic, NewsUpdate

router = APIRouter()
domain = NewsDomain()

# Потолок страницы: без него один запрос выгружает всю таблицу.
MAX_LIMIT = 50


@router.get("/", response_model=NewsListResponse)
async def list_news(
    response: Response,
    lang: str | None = Query(None, pattern="^(ru|uz|en)$"),
    limit: int = Query(20, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Публичная лента: только опубликованные, новые сверху."""
    items, total = await domain.list_news(db, lang=lang, limit=limit, offset=offset)
    response.headers["Cache-Control"] = (
        "public, max-age=30, s-maxage=120, stale-while-revalidate=300"
    )
    return {"items": items, "total": total}


# Литеральные пути ДО /{slug}: иначе "admin"/"by-id" уедут в slug.
@router.get("/admin", response_model=NewsListResponse)
async def list_news_admin(
    status: str | None = Query(None, pattern="^(draft|published)$"),
    lang: str | None = Query(None, pattern="^(ru|uz|en)$"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _owner: Profile = Depends(require_owner),
):
    """Админ-список: все статусы, без кэша."""
    items, total = await domain.list_news(
        db,
        lang=lang,
        status=status,
        include_unpublished=True,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "total": total}


@router.get("/by-id/{news_id}", response_model=NewsPublic)
async def get_news_by_id(
    news_id: int,
    db: AsyncSession = Depends(get_db),
    _owner: Profile = Depends(require_owner),
):
    """Для формы редактирования — любой статус, поэтому owner-only."""
    post = await domain.get_by_id(db, news_id)
    if not post:
        raise HTTPException(status_code=404, detail="News not found")
    return post


@router.get("/{slug}", response_model=NewsPublic)
async def get_news(slug: str, response: Response, db: AsyncSession = Depends(get_db)):
    """Публичная новость; черновики/отложенные неотличимы от 404."""
    post = await domain.get_published_by_slug(db, slug)
    if not post:
        raise HTTPException(status_code=404, detail="News not found")
    response.headers["Cache-Control"] = (
        "public, max-age=60, s-maxage=300, stale-while-revalidate=600"
    )
    return post


@router.post("/", response_model=NewsPublic, status_code=201)
async def create_news(
    news_in: NewsCreate,
    db: AsyncSession = Depends(get_db),
    owner: Profile = Depends(require_owner),
):
    return await domain.create_news(db, news_in, admin_id=owner.id)


@router.patch("/{news_id}", response_model=NewsPublic)
async def update_news(
    news_id: int,
    news_in: NewsUpdate,
    db: AsyncSession = Depends(get_db),
    _owner: Profile = Depends(require_owner),
):
    post = await domain.update_news(db, news_id, news_in)
    if not post:
        raise HTTPException(status_code=404, detail="News not found")
    return post


@router.delete("/{news_id}")
async def delete_news(
    news_id: int,
    db: AsyncSession = Depends(get_db),
    _owner: Profile = Depends(require_owner),
):
    success = await domain.delete_news(db, news_id)
    if not success:
        raise HTTPException(status_code=404, detail="News not found")
    return {"status": "deleted"}

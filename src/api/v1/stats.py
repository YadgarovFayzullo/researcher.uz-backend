from fastapi import Depends, APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from src.infrastructure.persistence.db import get_db
from src.domain.stats import StatsDomain
from src.schemas.stats import ArticleStatsResponse, ArticleInteractionCreate

router = APIRouter()
domain = StatsDomain


@router.get("/{article_id}", response_model=ArticleStatsResponse)
async def get_stats(article_id: int, db: AsyncSession = Depends(get_db)):
    """Получить агрегированную статистику статьи"""
    return await domain.get_article_stats(db, article_id)


@router.post("/record-view/{article_id}", response_model=ArticleStatsResponse)
async def record_view(article_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Записать просмотр статьи"""
    return await domain.record_view(db, article_id, request.client.host)


@router.post("/record-like/{article_id}", response_model=ArticleStatsResponse)
async def record_like(article_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Поставить лайк"""
    return await domain.record_like(db, article_id, request.client.host)

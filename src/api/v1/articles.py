from fastapi import Depends, APIRouter
from sqlalchemy.ext.asyncio import AsyncSession
from src.infrastructure.persistence.db import get_db
from src.domain.article import ArticleDomain
from src.schemas.article import ArticleCreate, ArticlePublic

router = APIRouter()
domain = ArticleDomain()

@router.get("/", response_model=list[ArticlePublic])
async def list_articles(db: AsyncSession = Depends(get_db)):
    """List all articles."""
    return await domain.list_articles(db)
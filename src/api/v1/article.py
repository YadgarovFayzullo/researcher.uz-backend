from fastapi import Depends, APIRouter, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.schemas.article import ArticleCreate, ArticleUpdate
from src.infrastructure.persistence.db import get_db
from src.domain.article import ArticleDomain

router = APIRouter()
domain = ArticleDomain()


@router.get("/{slug}")
async def get_article(slug: str, db: AsyncSession = Depends(get_db)):
    article = await domain.get_article_by_slug(db, slug)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return {"status": "ok", "article": article}


@router.post("/", status_code=201)
async def create_article(article_in: ArticleCreate, db: AsyncSession = Depends(get_db)):
    article = await domain.create_article(db, article_in)
    return {
        "status": "created",
        "slug": article.slug,
        "article": article
    }


@router.patch("/{id}")
async def update_article(id: int, article_in: ArticleUpdate, db: AsyncSession = Depends(get_db)):
    article = await domain.update_article(db, id, article_in)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return {"status": "updated", "article": article}


@router.delete("/{id}")
async def delete_article(id: int, db: AsyncSession = Depends(get_db)):
    deleted = await domain.delete_article(db, id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Article not found")
    return {"status": "deleted"}
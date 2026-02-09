from fastapi import APIRouter
from src.api.v1 import journals, journal, articles, stats, article

api_router = APIRouter()

api_router.include_router(journals.router, prefix="/journals", tags=["journals"])
api_router.include_router(journal.router, prefix="/journal", tags=["journal"])
api_router.include_router(articles.router, prefix="/articles", tags=["articles"])
api_router.include_router(stats.router, prefix="/stats", tags=["stats"])
api_router.include_router(article.router, prefix="/article", tags=["article"])

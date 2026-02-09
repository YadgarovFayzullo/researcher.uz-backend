from pydantic import BaseModel, ConfigDict
from datetime import datetime
from uuid import UUID


class ArticleStatsResponse(BaseModel):
    """Агрегированная статистика статьи"""
    article_id: int
    views: int = 0
    downloads: int = 0
    likes: int = 0
    dislikes: int = 0


class ArticleInteractionCreate(BaseModel):
    """Создание записи взаимодействия"""
    article_id: int
    ip_address: str | None = None
    view: int | None = None
    download: int | None = None
    like: int | None = None
    dislike: int | None = None


class ArticleInteractionPublic(BaseModel):
    """Публичная модель взаимодействия"""
    id: UUID
    article_id: int | None
    ip_address: str | None
    created_at: datetime | None
    view: float | None
    download: float | None
    like: float | None
    dislike: float | None

    model_config = ConfigDict(from_attributes=True)

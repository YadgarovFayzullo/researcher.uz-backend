from pydantic import BaseModel, ConfigDict
from datetime import datetime
from uuid import UUID


class ArticleIdsRequest(BaseModel):
    article_ids: list[int] = []


class JournalIdsRequest(BaseModel):
    journal_ids: list[int] = []
    days_back: int = 30


class AddInteractionRequest(BaseModel):
    article_id: int
    ip_address: str | None = None
    interaction_type: str  # view | download | like | dislike


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


# ------------------------------- живая статистика ------------------------- #


class LiveMinuteBucket(BaseModel):
    """Одна минута окна. Пустые минуты тоже присутствуют — иначе график врёт:
    провал без данных выглядел бы как продолжение предыдущего значения."""

    at: datetime
    views: int = 0
    downloads: int = 0


class LiveRecentRow(BaseModel):
    """Событие для ленты «читают сейчас»."""

    article_id: int | None = None
    title: str | None = None
    slug: str | None = None
    journal: str | None = None
    kind: str  # view | download
    at: datetime


class LiveTotals(BaseModel):
    views: int = 0
    downloads: int = 0
    visitors: int = 0


class LiveStatsResponse(BaseModel):
    """Ответ GET /stats/live."""

    now: datetime
    window_minutes: int
    # Уникальные адреса за последние ACTIVE_WINDOW минут — «сколько человек на
    # сайте прямо сейчас».
    active_readers: int = 0
    window: LiveTotals
    today: LiveTotals
    minutes: list[LiveMinuteBucket] = []
    recent: list[LiveRecentRow] = []

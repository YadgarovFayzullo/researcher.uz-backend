from fastapi import Depends, APIRouter, Request
from sqlalchemy.ext.asyncio import AsyncSession
from src.infrastructure.persistence.db import get_db
from src.core.bots import counts_as_human
from src.domain.stats import StatsDomain
from src.schemas.stats import (
    AddInteractionRequest,
    ArticleIdsRequest,
    ArticleStatsResponse,
    JournalIdsRequest,
)

router = APIRouter()
domain = StatsDomain


# ------------------------- батч-агрегаты (RPC-паритет) -------------------- #
@router.post("/articles")
async def article_stats_batch(body: ArticleIdsRequest, db: AsyncSession = Depends(get_db)):
    """get_article_stats(bigint[]) — {article_id, views, downloads} по набору статей."""
    return await domain.get_article_stats_batch(db, body.article_ids)


@router.get("/journals")
async def journal_stats(db: AsyncSession = Depends(get_db)):
    """get_journal_stats() — {journal_id, views, downloads} по всем журналам."""
    return await domain.get_journal_stats(db)


@router.get("/journals-overview")
async def journals_overview(db: AsyncSession = Depends(get_db)):
    """Порт вью journal_stats_view — счётчики по каждому журналу, включая пустые."""
    return await domain.get_journals_overview(db)


@router.get("/platform")
async def platform_stats(db: AsyncSession = Depends(get_db)):
    """get_platform_stats() — {totalViews, totalDownloads, totalIssues} без демо."""
    return await domain.get_platform_stats(db)


@router.post("/journal-analytics")
async def journal_analytics(body: JournalIdsRequest, db: AsyncSession = Depends(get_db)):
    return await domain.get_journal_analytics(db, body.journal_ids)


@router.post("/top-articles")
async def top_articles(body: JournalIdsRequest, db: AsyncSession = Depends(get_db)):
    return await domain.get_top_articles(db, body.journal_ids, body.days_back)


@router.post("/daily-stats")
async def daily_stats(body: JournalIdsRequest, db: AsyncSession = Depends(get_db)):
    return await domain.get_daily_stats(db, body.journal_ids, body.days_back)


# ------------------------------- мутации --------------------------------- #
@router.post("/interaction")
async def add_interaction(
    body: AddInteractionRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    """add_interaction — view/download/like/dislike. IP берём ТОЛЬКО из
    соединения (за uvicorn --proxy-headers это реальный клиент). body.ip_address
    намеренно игнорируем: иначе подстановка IP в теле обходит дедуп и накручивает
    счётчики.

    Роботов не записываем (см. src/core/bots.py): просмотр засчитывается из
    браузера, а краулеры исполняют JS — без фильтра статистика статей состоит из
    них на три четверти."""
    if not counts_as_human(request):
        return {"ok": False}

    ip = request.client.host if request.client else None
    ok = await domain.add_interaction(
        db,
        article_id=body.article_id,
        ip_address=ip,
        interaction_type=body.interaction_type,
    )
    return {"ok": ok}


@router.post("/increment-views/{article_id}")
async def increment_views(
    article_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    if not counts_as_human(request):
        return {"ok": False}
    await domain.increment_article_views(db, article_id)
    return {"ok": True}


# ------------------------- single-article (публичное) -------------------- #
@router.get("/{article_id}", response_model=ArticleStatsResponse)
async def get_stats(article_id: int, db: AsyncSession = Depends(get_db)):
    """Агрегированная статистика одной статьи (views/downloads/likes/dislikes)."""
    return await domain.get_article_stats(db, article_id)


@router.post("/record-view/{article_id}", response_model=ArticleStatsResponse)
async def record_view(article_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    # Роботу отдаём текущие счётчики, но его просмотр не записываем.
    if not counts_as_human(request):
        return await domain.get_article_stats(db, article_id)
    ip = request.client.host if request.client else None
    return await domain.record_view(db, article_id, ip)


@router.post("/record-like/{article_id}", response_model=ArticleStatsResponse)
async def record_like(article_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    ip = request.client.host if request.client else None
    return await domain.record_like(db, article_id, ip)

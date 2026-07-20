"""Статистика — таблица article_interactions + агрегаты (порт stats-RPC).

Часть RPC определена в репозитории (get_article_stats — article_stats.sql),
часть жила только в проде (get_journal_stats / get_platform_stats /
get_journal_analytics / get_top_articles / get_daily_stats / add_interaction /
increment_article_views) — их семантика восстановлена по вызовам во фронте
(useAnalytics.ts, StatisticsSection, JournalsSwiper, journal page).

Соглашение: строка взаимодействия несёт ОДИН признак = 1 (view|download|like|
dislike), поэтому «views» = count(*) filter (where view = 1) (как в SQL).
"""
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.persistence.models import (
    Article,
    ArticleInteraction,
    Issue,
    Journal,
)

# count(*) filter (where <col> = 1) через SUM(CASE ...)
_VIEW = func.coalesce(func.sum(case((ArticleInteraction.view == 1, 1), else_=0)), 0)
_DL = func.coalesce(func.sum(case((ArticleInteraction.download == 1, 1), else_=0)), 0)


class StatsDomain:
    """Работа со статистикой статей через таблицу article_interactions"""

    # ===================== существующие (single-article) =====================
    @staticmethod
    async def get_article_stats(db: AsyncSession, article_id: int) -> dict:
        """Агрегированная статистика по одной статье (для публичной страницы)."""
        result = await db.execute(
            select(ArticleInteraction).where(ArticleInteraction.article_id == article_id)
        )
        interactions = result.scalars().all()

        return {
            "article_id": article_id,
            "views": int(sum((i.view or 0) for i in interactions)),
            "downloads": int(sum((i.download or 0) for i in interactions)),
            "likes": int(sum((i.like or 0) for i in interactions)),
            "dislikes": int(sum((i.dislike or 0) for i in interactions)),
        }

    @staticmethod
    async def record_view(db: AsyncSession, article_id: int, ip_address: str) -> dict:
        """Записать просмотр статьи (антиспам по IP — 1 час)."""
        one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        result = await db.execute(
            select(ArticleInteraction).where(
                and_(
                    ArticleInteraction.article_id == article_id,
                    ArticleInteraction.ip_address == ip_address,
                    ArticleInteraction.created_at > one_hour_ago,
                )
            )
        )
        if result.scalar_one_or_none():
            return await StatsDomain.get_article_stats(db, article_id)

        db.add(ArticleInteraction(article_id=article_id, ip_address=ip_address, view=1))
        await db.commit()
        return await StatsDomain.get_article_stats(db, article_id)

    @staticmethod
    async def record_like(db: AsyncSession, article_id: int, ip_address: str) -> dict:
        db.add(ArticleInteraction(article_id=article_id, ip_address=ip_address, like=1))
        await db.commit()
        return await StatsDomain.get_article_stats(db, article_id)

    @staticmethod
    async def record_download(db: AsyncSession, article_id: int, ip_address: str) -> dict:
        db.add(ArticleInteraction(article_id=article_id, ip_address=ip_address, download=1))
        await db.commit()
        return await StatsDomain.get_article_stats(db, article_id)

    # ===================== RPC-паритет (батч-агрегаты) =======================
    @staticmethod
    async def get_article_stats_batch(
        db: AsyncSession, article_ids: list[int]
    ) -> list[dict]:
        """Порт get_article_stats(bigint[]): {article_id, views, downloads}.

        Только статьи, у которых есть взаимодействия (group by article_id).
        """
        if not article_ids:
            return []
        rows = (
            await db.execute(
                select(
                    ArticleInteraction.article_id,
                    _VIEW.label("views"),
                    _DL.label("downloads"),
                )
                .where(ArticleInteraction.article_id.in_(article_ids))
                .group_by(ArticleInteraction.article_id)
            )
        ).all()
        return [
            {"article_id": r.article_id, "views": int(r.views), "downloads": int(r.downloads)}
            for r in rows
        ]

    @staticmethod
    async def get_journal_stats(db: AsyncSession) -> list[dict]:
        """Порт get_journal_stats(): {journal_id, views, downloads} по всем журналам.

        Агрегирует interactions через article → issue → journal.
        """
        rows = (
            await db.execute(
                select(
                    Issue.journal_id.label("journal_id"),
                    _VIEW.label("views"),
                    _DL.label("downloads"),
                )
                .select_from(ArticleInteraction)
                .join(Article, Article.id == ArticleInteraction.article_id)
                .join(Issue, Issue.id == Article.issue_id)
                .where(Issue.journal_id.isnot(None))
                .group_by(Issue.journal_id)
            )
        ).all()
        return [
            {"journal_id": r.journal_id, "views": int(r.views), "downloads": int(r.downloads)}
            for r in rows
        ]

    @staticmethod
    async def get_journals_overview(db: AsyncSession) -> list[dict]:
        """Порт вью `journal_stats_view`: по КАЖДОМУ журналу
        {journal_id, total_views, total_downloads, total_articles, total_issues}.

        Отличается от get_journal_stats двумя вещами, и обе важны для каталога:
        считает ещё статьи и выпуски, и не теряет журналы без взаимодействий —
        внешний запрос идёт от Journal, а счётчики висят коррелированными
        подзапросами, поэтому свежий журнал отдаётся с нулями, а не пропадает.
        """
        # Связь с журналом выражаем join'ом до Issue, а не вложенным
        # `issue_id IN (select ... where journal_id = Journal.id)`: во втором
        # случае корреляция с Journal внутрь не пробрасывается, и каждый журнал
        # получает итог по всей платформе.
        def _interactions(flag) -> Any:
            return (
                select(func.count(ArticleInteraction.id))
                .select_from(ArticleInteraction)
                .join(Article, Article.id == ArticleInteraction.article_id)
                .join(Issue, Issue.id == Article.issue_id)
                .where(Issue.journal_id == Journal.id, flag == 1)
                .correlate(Journal)
                .scalar_subquery()
            )

        views_sq = _interactions(ArticleInteraction.view)
        downloads_sq = _interactions(ArticleInteraction.download)
        articles_sq = (
            select(func.count(Article.id))
            .select_from(Article)
            .join(Issue, Issue.id == Article.issue_id)
            .where(Issue.journal_id == Journal.id)
            .correlate(Journal)
            .scalar_subquery()
        )
        issues_sq = (
            select(func.count(Issue.id))
            .where(Issue.journal_id == Journal.id)
            .correlate(Journal)
            .scalar_subquery()
        )

        rows = (
            await db.execute(
                select(
                    Journal.id.label("journal_id"),
                    views_sq.label("total_views"),
                    downloads_sq.label("total_downloads"),
                    articles_sq.label("total_articles"),
                    issues_sq.label("total_issues"),
                )
            )
        ).all()
        return [
            {
                "journal_id": r.journal_id,
                "total_views": int(r.total_views or 0),
                "total_downloads": int(r.total_downloads or 0),
                "total_articles": int(r.total_articles or 0),
                "total_issues": int(r.total_issues or 0),
            }
            for r in rows
        ]

    @staticmethod
    async def get_platform_stats(db: AsyncSession) -> dict:
        """Порт get_platform_stats(): {totalViews, totalDownloads} по всей платформе."""
        row = (
            await db.execute(select(_VIEW.label("v"), _DL.label("d")))
        ).one()
        return {"totalViews": int(row.v), "totalDownloads": int(row.d)}

    @staticmethod
    async def get_journal_analytics(
        db: AsyncSession, journal_ids: list[int]
    ) -> list[dict]:
        """Порт get_journal_analytics(journal_ids):
        {journal_id, journal_name, total_views, total_downloads, total_articles}.
        """
        if not journal_ids:
            return []

        # Просмотры/скачивания по журналам (только для запрошенных).
        stat_rows = (
            await db.execute(
                select(
                    Issue.journal_id.label("jid"),
                    _VIEW.label("views"),
                    _DL.label("downloads"),
                )
                .select_from(ArticleInteraction)
                .join(Article, Article.id == ArticleInteraction.article_id)
                .join(Issue, Issue.id == Article.issue_id)
                .where(Issue.journal_id.in_(journal_ids))
                .group_by(Issue.journal_id)
            )
        ).all()
        stats = {r.jid: (int(r.views), int(r.downloads)) for r in stat_rows}

        # Кол-во статей по журналам.
        art_rows = (
            await db.execute(
                select(Issue.journal_id, func.count(Article.id))
                .select_from(Article)
                .join(Issue, Issue.id == Article.issue_id)
                .where(Issue.journal_id.in_(journal_ids))
                .group_by(Issue.journal_id)
            )
        ).all()
        art_counts = {jid: int(c) for jid, c in art_rows}

        # Имена журналов.
        name_rows = (
            await db.execute(
                select(Journal.id, Journal.name).where(Journal.id.in_(journal_ids))
            )
        ).all()
        names = {jid: name for jid, name in name_rows}

        out: list[dict] = []
        for jid in journal_ids:
            views, downloads = stats.get(jid, (0, 0))
            out.append(
                {
                    "journal_id": jid,
                    "journal_name": names.get(jid),
                    "total_views": views,
                    "total_downloads": downloads,
                    "total_articles": art_counts.get(jid, 0),
                }
            )
        return out

    @staticmethod
    async def get_top_articles(
        db: AsyncSession, journal_ids: list[int], days_back: int = 30
    ) -> list[dict]:
        """Порт get_top_articles(journal_ids, days_back): топ-10 статей по просмотрам
        за окно. Строка: {article_title, article_slug, journal_name, views_count, downloads_count}.
        """
        if not journal_ids:
            return []
        since = datetime.now(timezone.utc) - timedelta(days=days_back)
        rows = (
            await db.execute(
                select(
                    Article.title.label("title"),
                    Article.slug.label("slug"),
                    Journal.name.label("journal_name"),
                    _VIEW.label("views"),
                    _DL.label("downloads"),
                )
                .select_from(ArticleInteraction)
                .join(Article, Article.id == ArticleInteraction.article_id)
                .join(Issue, Issue.id == Article.issue_id)
                .join(Journal, Journal.id == Issue.journal_id)
                .where(
                    Issue.journal_id.in_(journal_ids),
                    ArticleInteraction.created_at >= since,
                )
                .group_by(Article.id, Article.title, Article.slug, Journal.name)
                .order_by(_VIEW.desc())
                .limit(10)
            )
        ).all()
        return [
            {
                "article_title": r.title,
                "article_slug": r.slug,
                "journal_name": r.journal_name,
                "views_count": int(r.views),
                "downloads_count": int(r.downloads),
            }
            for r in rows
        ]

    @staticmethod
    async def get_daily_stats(
        db: AsyncSession, journal_ids: list[int], days_back: int = 30
    ) -> list[dict]:
        """Порт get_daily_stats(journal_ids, days_back): {date, views_count, downloads_count}
        по дням внутри окна, отсортировано по дате.
        """
        if not journal_ids:
            return []
        since = datetime.now(timezone.utc) - timedelta(days=days_back)
        day = func.date(ArticleInteraction.created_at).label("date")
        rows = (
            await db.execute(
                select(day, _VIEW.label("views"), _DL.label("downloads"))
                .select_from(ArticleInteraction)
                .join(Article, Article.id == ArticleInteraction.article_id)
                .join(Issue, Issue.id == Article.issue_id)
                .where(
                    Issue.journal_id.in_(journal_ids),
                    ArticleInteraction.created_at >= since,
                )
                .group_by(day)
                .order_by(day)
            )
        ).all()
        return [
            {
                "date": r.date.isoformat() if r.date else None,
                "views_count": int(r.views),
                "downloads_count": int(r.downloads),
            }
            for r in rows
        ]

    # ===================== мутации (порт add_interaction) ====================
    @staticmethod
    async def increment_article_views(db: AsyncSession, article_id: int) -> None:
        """Порт increment_article_views(article_id): просто плюс один просмотр."""
        db.add(ArticleInteraction(article_id=article_id, view=1))
        await db.commit()

    @staticmethod
    async def add_interaction(
        db: AsyncSession,
        *,
        article_id: int,
        ip_address: str | None,
        interaction_type: str,
    ) -> bool:
        """Порт add_interaction(p_article_id, p_ip_address, p_interaction_type).

        Возвращает False, если действие отклонено (повторный просмотр с того же IP
        в течение часа), иначе True. Для like/dislike реакция одна на IP+статью —
        прежние реакции этого IP снимаются перед вставкой новой.
        """
        if interaction_type not in ("view", "download", "like", "dislike"):
            return False

        if interaction_type == "view":
            one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
            recent = (
                await db.execute(
                    select(ArticleInteraction.id).where(
                        ArticleInteraction.article_id == article_id,
                        ArticleInteraction.ip_address == ip_address,
                        ArticleInteraction.created_at > one_hour_ago,
                        ArticleInteraction.view == 1,
                    )
                )
            ).scalars().first()
            if recent is not None:
                return False
            db.add(
                ArticleInteraction(article_id=article_id, ip_address=ip_address, view=1)
            )
            await db.commit()
            return True

        if interaction_type in ("like", "dislike"):
            # одна реакция на IP+статью: снять прошлые like/dislike этого IP
            await db.execute(
                ArticleInteraction.__table__.delete().where(
                    ArticleInteraction.article_id == article_id,
                    ArticleInteraction.ip_address == ip_address,
                    (ArticleInteraction.like == 1) | (ArticleInteraction.dislike == 1),
                )
            )
            kwargs = {interaction_type: 1}
            db.add(
                ArticleInteraction(
                    article_id=article_id, ip_address=ip_address, **kwargs
                )
            )
            await db.commit()
            return True

        # download
        db.add(
            ArticleInteraction(article_id=article_id, ip_address=ip_address, download=1)
        )
        await db.commit()
        return True

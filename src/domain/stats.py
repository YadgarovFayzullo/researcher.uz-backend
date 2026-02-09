from datetime import datetime, timezone, timedelta
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from src.infrastructure.persistence.models import ArticleInteraction


class StatsDomain:
    """Работа со статистикой статей через таблицу article_interactions"""
    
    @staticmethod
    async def get_article_stats(db: AsyncSession, article_id: int) -> dict:
        """Получить агрегированную статистику по статье"""
        result = await db.execute(
            select(ArticleInteraction).where(ArticleInteraction.article_id == article_id)
        )
        interactions = result.scalars().all()
        
        total_views = sum((i.view or 0) for i in interactions)
        total_downloads = sum((i.download or 0) for i in interactions)
        total_likes = sum((i.like or 0) for i in interactions)
        total_dislikes = sum((i.dislike or 0) for i in interactions)
        
        return {
            "article_id": article_id,
            "views": int(total_views),
            "downloads": int(total_downloads),
            "likes": int(total_likes),
            "dislikes": int(total_dislikes)
        }

    @staticmethod
    async def record_view(db: AsyncSession, article_id: int, ip_address: str) -> dict:
        """Записать просмотр статьи (с защитой от спама по IP)"""
        one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        
        # Проверяем, был ли просмотр с этого IP за последний час
        result = await db.execute(
            select(ArticleInteraction).where(
                and_(
                    ArticleInteraction.article_id == article_id,
                    ArticleInteraction.ip_address == ip_address,
                    ArticleInteraction.created_at > one_hour_ago
                )
            )
        )
        recent_interaction = result.scalar_one_or_none()
        
        # Если просмотр был менее часа назад — не считаем
        if recent_interaction:
            return await StatsDomain.get_article_stats(db, article_id)
        
        # Записываем новый просмотр
        new_interaction = ArticleInteraction(
            article_id=article_id,
            ip_address=ip_address,
            view=1
        )
        db.add(new_interaction)
        await db.commit()
        
        return await StatsDomain.get_article_stats(db, article_id)

    @staticmethod
    async def record_like(db: AsyncSession, article_id: int, ip_address: str) -> dict:
        """Записать лайк статьи"""
        new_interaction = ArticleInteraction(
            article_id=article_id,
            ip_address=ip_address,
            like=1
        )
        db.add(new_interaction)
        await db.commit()
        
        return await StatsDomain.get_article_stats(db, article_id)

    @staticmethod
    async def record_download(db: AsyncSession, article_id: int, ip_address: str) -> dict:
        """Записать скачивание PDF"""
        new_interaction = ArticleInteraction(
            article_id=article_id,
            ip_address=ip_address,
            download=1
        )
        db.add(new_interaction)
        await db.commit()
        
        return await StatsDomain.get_article_stats(db, article_id)
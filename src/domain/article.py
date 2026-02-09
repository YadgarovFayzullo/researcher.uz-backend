import uuid
from datetime import datetime, timezone
from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.infrastructure.persistence.models import Article
from src.schemas.article import ArticleCreate

class ArticleDomain:
    def validate_publication(self, title: str) -> bool:
        # Rules for publication
        return bool(title and len(title) > 5)

    def generate_slug(self, text: str) -> str:
        """
        Converts text (including Cyrillic) to a URL-friendly slug.
        Example: 'Древняя Греция' -> 'drevniaia-gretsiia'
        """
        return slugify(text)

    def get_current_time(self) -> datetime:
        """Returns the current UTC time."""
        return datetime.now(timezone.utc)

    async def list_articles(self, db: AsyncSession):
        """
        Retrieves all articles from the database.
        """
        result = await db.execute(select(Article))
        return result.scalars().all()

    def search_logic(self, query: str):
        # Search implementation
        pass


    async def get_article_by_slug(self, db: AsyncSession, slug: str):
        result = await db.execute(select(Article).where(Article.slug == slug))
        return result.scalars().first()


    async def create_article(self, db: AsyncSession, article_in: ArticleCreate) -> Article:
        slug = self.generate_slug(article_in.title)
        existing = await self.get_article_by_slug(db, slug)
        if existing:
            slug = f"{slug}-{str(uuid.uuid4())[:6]}" 

        created_at = self.get_current_time()

        new_article = Article(
            **article_in.model_dump(),
            slug=slug,
            created_at=created_at
        )

        db.add(new_article)
        await db.commit()
        await db.refresh(new_article)

        return new_article
        
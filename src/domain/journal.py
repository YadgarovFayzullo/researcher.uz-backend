import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from slugify import slugify
from src.infrastructure.persistence.models import Journal
from src.schemas.journal import JournalCreate, JournalUpdate

class JournalDomain:
    def generate_slug(self, text: str) -> str:
        """
        Converts text to a URL-friendly slug.
        """
        return slugify(text)

    async def list_journals(self, db: AsyncSession):
        """
        Retrieves all journals from the database.
        """
        result = await db.execute(select(Journal))
        return result.scalars().all()

    async def get_journal_by_slug(self, db: AsyncSession, slug: str):
        """
        Retrieves a journal by its slug.
        """
        result = await db.execute(select(Journal).where(Journal.slug == slug))
        return result.scalars().first()

    async def get_journal_by_id(self, db: AsyncSession, journal_id: int):
        """
        Retrieves a journal by its ID.
        """
        result = await db.execute(select(Journal).where(Journal.id == journal_id))
        return result.scalars().first()

    async def create_journal(self, db: AsyncSession, journal_in: JournalCreate) -> Journal:
        """
        Creates a new journal with unique slug.
        """
        slug = self.generate_slug(journal_in.name)
        existing = await self.get_journal_by_slug(db, slug)
        if existing:
            slug = f"{slug}-{str(uuid.uuid4())[:6]}"

        new_journal = Journal(
            **journal_in.model_dump(),
            slug=slug
        )

        db.add(new_journal)
        await db.commit()
        await db.refresh(new_journal)

        return new_journal

    async def update_journal(self, db: AsyncSession, journal_id: int, journal_in: JournalUpdate) -> Journal | None:
        """
        Updates an existing journal.
        """
        journal = await self.get_journal_by_id(db, journal_id)
        if not journal:
            return None

        update_data = journal_in.model_dump(exclude_unset=True)
        
        # Update slug if name changes
        if "name" in update_data:
            new_slug = self.generate_slug(update_data["name"])
            if new_slug != journal.slug:
                existing = await self.get_journal_by_slug(db, new_slug)
                if existing:
                    new_slug = f"{new_slug}-{str(uuid.uuid4())[:6]}"
                journal.slug = new_slug

        for field, value in update_data.items():
            setattr(journal, field, value)

        await db.commit()
        await db.refresh(journal)
        return journal

    async def delete_journal(self, db: AsyncSession, journal_id: int) -> bool:
        """
        Deletes a journal by ID.
        """
        journal = await self.get_journal_by_id(db, journal_id)
        if not journal:
            return False
        
        await db.delete(journal)
        await db.commit()
        return True

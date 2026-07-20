import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from slugify import slugify
from src.infrastructure.persistence.models import Issue, Journal
from src.schemas.journal import JournalCreate, JournalPublic, JournalUpdate

class JournalDomain:
    def generate_slug(self, text: str) -> str:
        """
        Converts text to a URL-friendly slug.
        """
        return slugify(text)

    async def list_journals(
        self,
        db: AsyncSession,
        *,
        type_: str | None = None,
        with_issue_counts: bool = False,
    ) -> list[dict[str, Any]]:
        """Список журналов, по умолчанию — новые сверху.

        `type_` отделяет обычные журналы от серий конференций (каталог
        /conferences показывает только вторые). `with_issue_counts` добавляет
        число выпусков коррелированным подзапросом, а не отдельным запросом на
        каждый журнал — счётчик нужен карточкам каталога.
        """
        issue_count = (
            select(func.count(Issue.id))
            .where(Issue.journal_id == Journal.id)
            .correlate(Journal)
            .scalar_subquery()
        )

        stmt = select(Journal)
        if with_issue_counts:
            stmt = stmt.add_columns(issue_count.label("issues_count"))
        if type_ is not None:
            stmt = stmt.where(Journal.type == type_)
        stmt = stmt.order_by(Journal.created_at.desc().nullslast(), Journal.id.desc())

        rows = (await db.execute(stmt)).all()
        out: list[dict[str, Any]] = []
        for row in rows:
            journal = row[0]
            # Раскладываем через схему, а не обходом колонок таблицы: в БД
            # колонка зовётся "metadata", и getattr(journal, "metadata") вернул
            # бы служебный Base.metadata вместо JSONB. Схема уже знает про
            # алиас meta/metadata — не дублируем это знание здесь.
            data = JournalPublic.model_validate(journal).model_dump()
            # Ключ наружу зовётся issues_count и всегда присутствует: иначе
            # карточке пришлось бы отличать «нет выпусков» от «не запрашивали».
            data["issues_count"] = int(row[1]) if with_issue_counts else 0
            out.append(data)
        return out

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
        """Создать журнал, обеспечив уникальность слуга."""
        data = journal_in.model_dump(exclude_unset=True, by_alias=False)
        # slug приходит в теле; если его не прислали — выводим из названия.
        # Достаём его из данных, иначе Journal(**data, slug=...) получит два
        # значения одного аргумента и упадёт с TypeError.
        slug = data.pop("slug", None) or self.generate_slug(journal_in.name)
        if await self.get_journal_by_slug(db, slug):
            slug = f"{slug}-{str(uuid.uuid4())[:6]}"

        new_journal = Journal(**data, slug=slug)
        db.add(new_journal)
        await db.commit()
        await db.refresh(new_journal)
        return new_journal

    async def update_journal(self, db: AsyncSession, journal_id: int, journal_in: JournalUpdate) -> Journal | None:
        """Обновить журнал.

        Slug намеренно не пересчитывается при смене названия: он входит в
        публичные и канонические URL (/uz/journal/<slug>), по которым журнал уже
        проиндексирован. Молчаливая смена слуга положила бы все внешние ссылки.
        """
        journal = await self.get_journal_by_id(db, journal_id)
        if not journal:
            return None

        for field, value in journal_in.model_dump(
            exclude_unset=True, by_alias=False
        ).items():
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

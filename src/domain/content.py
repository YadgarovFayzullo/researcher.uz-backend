"""Под-ресурсы контента: секции конференций, авторы, пристатейные ссылки,
библиотека пользователя.

Порт запросов фронта: ConferenceAdmin/AddArticle (секции), lib/articleAuthors.ts
и researcher/[orcid] (авторы), lib/articleReferences.ts и CitationsDashboard
(ссылки), SaveButton/LibraryPage (библиотека).
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.domain.serialization import HEAVY_ARTICLE_COLUMNS, row_to_dict
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    ArticleInteraction,
    ArticleReference,
    ConferenceSection,
    Issue,
    Journal,
    SavedArticle,
)
from src.schemas.content import (
    AuthorIn,
    ReferenceIn,
    SectionCreate,
    SectionUpdate,
)


class SectionDomain:
    async def list_sections(
        self, db: AsyncSession, issue_id: int
    ) -> list[ConferenceSection]:
        res = await db.execute(
            select(ConferenceSection)
            .where(ConferenceSection.issue_id == issue_id)
            .order_by(ConferenceSection.position, ConferenceSection.id)
        )
        return list(res.scalars().all())

    async def get_section(
        self, db: AsyncSession, section_id: int
    ) -> ConferenceSection | None:
        res = await db.execute(
            select(ConferenceSection).where(ConferenceSection.id == section_id)
        )
        return res.scalars().first()

    async def create_section(
        self, db: AsyncSession, section_in: SectionCreate
    ) -> ConferenceSection:
        section = ConferenceSection(**section_in.model_dump())
        db.add(section)
        await db.commit()
        await db.refresh(section)
        return section

    async def update_section(
        self, db: AsyncSession, section_id: int, section_in: SectionUpdate
    ) -> ConferenceSection | None:
        section = await self.get_section(db, section_id)
        if not section:
            return None
        for field, value in section_in.model_dump(exclude_unset=True).items():
            setattr(section, field, value)
        await db.commit()
        await db.refresh(section)
        return section

    async def delete_section(self, db: AsyncSession, section_id: int) -> bool:
        section = await self.get_section(db, section_id)
        if not section:
            return False
        # Доклады не удаляем — открепляем (articles.section_id nullable).
        await db.execute(
            Article.__table__.update()
            .where(Article.section_id == section_id)
            .values(section_id=None)
        )
        await db.delete(section)
        await db.commit()
        return True

    async def reorder(
        self, db: AsyncSession, issue_id: int, section_ids: list[int]
    ) -> list[ConferenceSection]:
        """Применить порядок одной транзакцией. Чужие для тома id игнорируем,
        не упавшие в список секции уезжают в конец с сохранением порядка."""
        current = await self.list_sections(db, issue_id)
        by_id = {s.id: s for s in current}
        ordered = [by_id[i] for i in section_ids if i in by_id]
        ordered += [s for s in current if s.id not in set(section_ids)]
        for pos, section in enumerate(ordered):
            section.position = pos
        await db.commit()
        return ordered


class AuthorDomain:
    async def list_by_article(
        self, db: AsyncSession, article_id: int
    ) -> list[ArticleAuthor]:
        res = await db.execute(
            select(ArticleAuthor)
            .where(ArticleAuthor.article_id == article_id)
            .order_by(ArticleAuthor.author_order)
        )
        return list(res.scalars().all())

    async def replace_for_article(
        self, db: AsyncSession, article_id: int, authors: list[AuthorIn]
    ) -> list[ArticleAuthor]:
        """Полная замена списка авторов (delete + insert), как в
        lib/articleAuthors.ts. Одна транзакция: статья не остаётся без авторов
        при сбое вставки."""
        await db.execute(
            delete(ArticleAuthor).where(ArticleAuthor.article_id == article_id)
        )
        rows = [
            ArticleAuthor(article_id=article_id, **a.model_dump()) for a in authors
        ]
        db.add_all(rows)
        await db.commit()
        return await self.list_by_article(db, article_id)

    async def list_publications_by_orcid(
        self, db: AsyncSession, orcid: str
    ) -> list[dict[str, Any]]:
        """Публикации автора по ORCID — форма AUTHOR_ARTICLE_SELECT фронта
        (статья + название журнала через issues -> journals)."""
        return await self._publications(db, ArticleAuthor.orcid == orcid)

    async def list_publications_by_profile(
        self, db: AsyncSession, profile_id: str
    ) -> list[dict[str, Any]]:
        """То же, но по владельцу профиля, а не по ORCID.

        Нужна страницам исследователей без ORCID (регистрация через Google):
        связь со статьёй у них держится не идентификатором, а привязкой
        `article_authors.profile_id`, которую ставит claim в кабинете.
        """
        try:
            uid = uuid.UUID(str(profile_id))
        except (TypeError, ValueError):
            return []
        return await self._publications(db, ArticleAuthor.profile_id == uid)

    async def _publications(
        self, db: AsyncSession, where: Any
    ) -> list[dict[str, Any]]:
        res = await db.execute(
            select(
                ArticleAuthor.author_name,
                ArticleAuthor.author_order,
                Article.id,
                Article.title,
                Article.slug,
                Article.publication_type,
                Article.publication_year,
                Article.data,
                Article.doi,
                Article.publisher,
                Journal.name.label("journal_name"),
            )
            .join(Article, Article.id == ArticleAuthor.article_id)
            .outerjoin(Issue, Issue.id == Article.issue_id)
            .outerjoin(Journal, Journal.id == Issue.journal_id)
            .where(where)
            .order_by(Article.data.desc().nullslast(), Article.id.desc())
        )
        return [
            {
                "author_name": r.author_name,
                "author_order": r.author_order,
                "article": {
                    "id": r.id,
                    "title": r.title,
                    "slug": r.slug,
                    "publication_type": r.publication_type,
                    "publication_year": r.publication_year,
                    "data": r.data,
                    "doi": r.doi,
                    "publisher": r.publisher,
                    "journal_name": r.journal_name,
                },
            }
            for r in res.all()
        ]


class ReferenceDomain:
    async def list_by_article(
        self, db: AsyncSession, article_id: int
    ) -> list[dict[str, Any]]:
        cited = aliased(Article)
        res = await db.execute(
            select(ArticleReference, cited.title.label("cited_title"))
            .outerjoin(cited, cited.id == ArticleReference.cited_article_id)
            .where(ArticleReference.article_id == article_id)
            .order_by(ArticleReference.position.asc().nullslast())
        )
        out: list[dict[str, Any]] = []
        for ref, cited_title in res.all():
            data = row_to_dict(ref, ArticleReference)
            data["cited_title"] = cited_title
            out.append(data)
        return out

    async def replace_for_article(
        self, db: AsyncSession, article_id: int, references: list[ReferenceIn]
    ) -> list[dict[str, Any]]:
        await db.execute(
            delete(ArticleReference).where(ArticleReference.article_id == article_id)
        )
        db.add_all(
            [
                ArticleReference(article_id=article_id, **r.model_dump())
                for r in references
            ]
        )
        await db.commit()
        return await self.list_by_article(db, article_id)

    async def citing_map(
        self, db: AsyncSession, cited_article_ids: list[int]
    ) -> dict[int, list[int]]:
        """{cited_article_id: [article_id, ...]} — кто ссылается на эти статьи
        (CitationsDashboard берёт это батчами по срезам)."""
        if not cited_article_ids:
            return {}
        res = await db.execute(
            select(ArticleReference.cited_article_id, ArticleReference.article_id)
            .where(ArticleReference.cited_article_id.in_(cited_article_ids))
            .distinct()
        )
        out: dict[int, list[int]] = {i: [] for i in cited_article_ids}
        for cited_id, article_id in res.all():
            out.setdefault(cited_id, []).append(article_id)
        return out


class LibraryDomain:
    async def list_saved(self, db: AsyncSession, user_id) -> list[dict[str, Any]]:
        """Сохранённые статьи с журналом и счётчиками просмотров/скачиваний —
        форма LibraryPage (saved_articles -> articles -> issues -> journals +
        article_interactions)."""
        views = func.coalesce(
            func.sum(case((ArticleInteraction.view == 1, 1), else_=0)), 0
        )
        downloads = func.coalesce(
            func.sum(case((ArticleInteraction.download == 1, 1), else_=0)), 0
        )
        res = await db.execute(
            select(
                SavedArticle.created_at.label("saved_at"),
                Article,
                Journal.name.label("journal_name"),
                Journal.slug.label("journal_slug"),
                views.label("views"),
                downloads.label("downloads"),
            )
            .join(Article, Article.id == SavedArticle.article_id)
            .outerjoin(Issue, Issue.id == Article.issue_id)
            .outerjoin(Journal, Journal.id == Issue.journal_id)
            .outerjoin(
                ArticleInteraction, ArticleInteraction.article_id == Article.id
            )
            .where(SavedArticle.user_id == user_id)
            .group_by(
                SavedArticle.created_at,
                Article.id,
                Journal.name,
                Journal.slug,
            )
            .order_by(SavedArticle.created_at.desc())
        )
        out = []
        for row in res.all():
            # embedding наружу не отдаём — это 768-мерный вектор поиска.
            data = row_to_dict(row.Article, Article, exclude=HEAVY_ARTICLE_COLUMNS)
            data["journal_name"] = row.journal_name
            data["journal_slug"] = row.journal_slug
            data["views"] = int(row.views or 0)
            data["downloads"] = int(row.downloads or 0)
            out.append({"saved_at": row.saved_at, "article": data})
        return out

    async def is_saved(self, db: AsyncSession, user_id, article_id: int) -> bool:
        res = await db.execute(
            select(SavedArticle.id).where(
                SavedArticle.user_id == user_id,
                SavedArticle.article_id == article_id,
            )
        )
        return res.first() is not None

    async def save(self, db: AsyncSession, user_id, article_id: int) -> bool:
        """Идемпотентно: повторное сохранение не создаёт дубль."""
        if await self.is_saved(db, user_id, article_id):
            return False
        db.add(SavedArticle(user_id=user_id, article_id=article_id))
        await db.commit()
        return True

    async def unsave(self, db: AsyncSession, user_id, article_id: int) -> bool:
        res = await db.execute(
            delete(SavedArticle).where(
                SavedArticle.user_id == user_id,
                SavedArticle.article_id == article_id,
            )
        )
        await db.commit()
        return (res.rowcount or 0) > 0

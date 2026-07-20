"""Выпуски журналов и тома конференций (`issues`).

Порт запросов фронта: JournalPage (выпуски журнала), JournalInfo (CRUD + счётчик
статей), DOIlist (годы журнала, выпуски по году), ConferenceEventPage (том).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.serialization import row_to_dict
from src.infrastructure.persistence.models import Article, ConferenceSection, Issue
from src.schemas.issue import IssueCreate, IssueUpdate


class IssueDomain:
    async def list_issues(
        self,
        db: AsyncSession,
        *,
        journal_id: int | None = None,
        year: int | None = None,
        with_counts: bool = True,
    ) -> list[dict[str, Any]]:
        """Выпуски (опц. одного журнала/года) + число статей в каждом.

        Счётчик — коррелированный подзапрос вместо GROUP BY: выпуски без статей
        должны отдаваться с нулём, а не пропадать (это `articles(count)` фронта).
        """
        count_sq = (
            select(func.count(Article.id))
            .where(Article.issue_id == Issue.id)
            .correlate(Issue)
            .scalar_subquery()
        )
        cols = [Issue, count_sq.label("article_count")] if with_counts else [Issue]
        stmt = select(*cols)
        if journal_id is not None:
            stmt = stmt.where(Issue.journal_id == journal_id)
        if year is not None:
            stmt = stmt.where(Issue.year == year)
        # created_at desc — порядок JournalInfo/JournalPage.
        stmt = stmt.order_by(Issue.created_at.desc().nullslast(), Issue.id.desc())

        rows = (await db.execute(stmt)).all()
        out: list[dict[str, Any]] = []
        for row in rows:
            data = row_to_dict(row[0], Issue)
            data["article_count"] = row[1] if with_counts else None
            out.append(data)
        return out

    async def get_issue(self, db: AsyncSession, issue_id: int) -> Issue | None:
        res = await db.execute(select(Issue).where(Issue.id == issue_id))
        return res.scalars().first()

    async def list_years(self, db: AsyncSession, journal_id: int) -> list[int]:
        """Годы, за которые у журнала есть выпуски (DOIlist, фильтр)."""
        res = await db.execute(
            select(distinct(Issue.year))
            .where(Issue.journal_id == journal_id, Issue.year.isnot(None))
            .order_by(Issue.year.desc())
        )
        return [r[0] for r in res.all()]

    async def create_issue(self, db: AsyncSession, issue_in: IssueCreate) -> Issue:
        issue = Issue(**issue_in.model_dump(by_alias=False))
        db.add(issue)
        await db.commit()
        await db.refresh(issue)
        return issue

    async def update_issue(
        self, db: AsyncSession, issue_id: int, issue_in: IssueUpdate
    ) -> Issue | None:
        issue = await self.get_issue(db, issue_id)
        if not issue:
            return None
        # exclude_unset: не затираем поля, которых не было в теле запроса.
        for field, value in issue_in.model_dump(exclude_unset=True).items():
            setattr(issue, field, value)
        await db.commit()
        await db.refresh(issue)
        return issue

    async def delete_issue(self, db: AsyncSession, issue_id: int) -> bool:
        issue = await self.get_issue(db, issue_id)
        if not issue:
            return False
        # Статьи выпуска не удаляем — открепляем (FK articles.issue_id nullable),
        # иначе удаление выпуска молча уносит опубликованный контент.
        await db.execute(
            Article.__table__.update()
            .where(Article.issue_id == issue_id)
            .values(issue_id=None, section_id=None)
        )
        # Секции конференции живут только внутри тома (FK issue_id NOT NULL),
        # осиротеть не могут — удаляем вместе с ним. Порядок важен: сначала
        # отвязали статьи от секций, иначе FK articles.section_id не даст.
        await db.execute(
            delete(ConferenceSection).where(ConferenceSection.issue_id == issue_id)
        )
        await db.delete(issue)
        await db.commit()
        return True

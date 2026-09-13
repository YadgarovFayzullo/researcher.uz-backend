"""Карточки авторов: публичная страница и присвоение («Это я»).

Карточка собирается скриптом `scripts/backfill_authors.py` из подписей под
статьями и живёт без аккаунта — у авторов импортированных работ его нет. Здесь
только чтение карточки и её присвоение владельцем.

Зачем присвоение вообще: claim — единственный вход в регистрацию, который у
платформы есть бесплатно. Человек находит в поиске страницу со своими статьями
и заводит аккаунт, чтобы ею управлять.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Integer, Text, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.content import AuthorDomain
from src.domain.demo import article_is_not_demo
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    Author,
    Issue,
    Journal,
    Profile,
)

# Ниже этого числа работ карточка не индексируется поисковиками: страниц-
# одиночек тысячи, и массовая выдача тонких страниц роняет доверие ко всему
# домену. Страница при этом открывается и работает — закрыт только индекс.
MIN_WORKS_FOR_INDEX = 2


class AuthorCardError(Exception):
    pass


class AuthorCardDomain:
    def __init__(self) -> None:
        self._authors = AuthorDomain()

    async def get_by_slug(self, db: AsyncSession, slug: str) -> dict[str, Any] | None:
        row = (
            await db.execute(select(Author).where(Author.slug == slug))
        ).scalar_one_or_none()
        if row is None:
            return None
        card = {
            "id": str(row.id),
            "slug": row.slug,
            "display_name": row.display_name,
            "orcid": row.orcid,
            "works_count": row.works_count,
            "profile_id": str(row.profile_id) if row.profile_id else None,
            "indexable": row.works_count >= MIN_WORKS_FOR_INDEX,
        }
        if row.profile_id:
            # Карточка присвоена: у человека уже есть канонический адрес
            # профиля, и страница автора должна увести на него, а не
            # показывать вторую версию той же личности.
            profile = (
                await db.execute(
                    select(Profile.id, Profile.orcid_id, Profile.full_name).where(
                        Profile.id == row.profile_id
                    )
                )
            ).first()
            if profile is not None:
                card["profile"] = {
                    "id": str(profile.id),
                    "orcid": profile.orcid_id,
                    "full_name": profile.full_name,
                }
        return card

    async def publications(
        self, db: AsyncSession, author_id: str
    ) -> list[dict[str, Any]]:
        """Работы карточки — только опубликованные и не из демо-журнала."""
        try:
            aid = uuid.UUID(str(author_id))
        except (TypeError, ValueError):
            return []
        rows = await self._authors._publications(
            db,
            (ArticleAuthor.author_id == aid)
            & (Article.published.is_(True))
            & article_is_not_demo(),
        )
        # Один человек иногда подписан в статье дважды (разные написания в
        # исходных данных) — в списке работ это выглядело бы дублем.
        seen: set[int] = set()
        out: list[dict[str, Any]] = []
        for row in rows:
            article_id = row["article"]["id"]
            if article_id in seen:
                continue
            seen.add(article_id)
            out.append(row)
        return out

    async def name_variants(self, db: AsyncSession, author_id: str) -> list[str]:
        """Все написания подписи в этой карточке.

        Показываем их на странице: человек должен видеть, почему «Xalilova Z.F.»
        и «Халилова Зилола Фарходовна» считаются одним автором, — и заметить,
        если слияние ошибочно.
        """
        try:
            aid = uuid.UUID(str(author_id))
        except (TypeError, ValueError):
            return []
        rows = (
            await db.execute(
                select(ArticleAuthor.author_name)
                .where(ArticleAuthor.author_id == aid)
                .distinct()
            )
        ).scalars().all()
        return sorted({(n or "").strip() for n in rows if (n or "").strip()})

    async def list_top(
        self, db: AsyncSession, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """Список карточек: указатель авторов, перелинковка и карта сайта.

        Только индексируемые: карточек с одной работой тысячи, и в списке они
        были бы шумом, а в индексе — тонкими страницами.

        Кроме имени отдаём то, по чему человека узнают: где печатается, за
        какие годы и сколько работ. Эти поля собираются ОДНИМ запросом с
        агрегацией, а не по запросу на карточку: на странице их две сотни.
        """
        where = Author.works_count >= MIN_WORKS_FOR_INDEX
        total = (
            await db.execute(select(func.count(Author.id)).where(where))
        ).scalar_one()

        page = (
            select(Author.id)
            .where(where)
            .order_by(Author.works_count.desc(), Author.display_name)
            .limit(min(limit, 500))
            .offset(offset)
            .subquery()
        )

        year = func.coalesce(
            Article.publication_year,
            func.nullif(func.substr(func.cast(Article.data, Text), 1, 4), "").cast(
                Integer
            ),
        )
        rows = (
            await db.execute(
                select(
                    Author.slug,
                    Author.display_name,
                    Author.works_count,
                    Author.orcid,
                    Author.profile_id,
                    Profile.avatar_url,
                    Profile.workplace,
                    # Журнал, где автор печатается чаще всего: одна строка
                    # говорит о человеке больше, чем список из десяти.
                    func.mode().within_group(Journal.name).label("main_journal"),
                    func.min(year).label("first_year"),
                    func.max(year).label("last_year"),
                )
                .select_from(Author)
                .join(page, page.c.id == Author.id)
                .outerjoin(ArticleAuthor, ArticleAuthor.author_id == Author.id)
                .outerjoin(
                    Article,
                    (Article.id == ArticleAuthor.article_id)
                    & Article.published.is_(True),
                )
                .outerjoin(Issue, Issue.id == Article.issue_id)
                .outerjoin(Journal, Journal.id == Issue.journal_id)
                .outerjoin(Profile, Profile.id == Author.profile_id)
                .group_by(
                    Author.id,
                    Author.slug,
                    Author.display_name,
                    Author.works_count,
                    Author.orcid,
                    Author.profile_id,
                    Profile.avatar_url,
                    Profile.workplace,
                )
                .order_by(Author.works_count.desc(), Author.display_name)
            )
        ).all()

        return {
            "total": total,
            "items": [
                {
                    "slug": r.slug,
                    "display_name": r.display_name,
                    "works_count": r.works_count,
                    "orcid": r.orcid,
                    "claimed": r.profile_id is not None,
                    "avatar_url": r.avatar_url,
                    "workplace": r.workplace,
                    "main_journal": r.main_journal,
                    "first_year": r.first_year,
                    "last_year": r.last_year,
                }
                for r in rows
            ],
        }

    async def cards_for_article(
        self, db: AsyncSession, article_id: int
    ) -> list[dict[str, Any]]:
        """Подписи под статьёй вместе со слагом карточки автора.

        Нужны странице статьи: до карточек имя автора вело в поиск, а он
        `noindex` — то есть перелинковки для робота не возникало вовсе.
        Подпись без карточки (имя из одного слова) отдаётся со slug = null и
        остаётся обычным текстом.
        """
        rows = (
            await db.execute(
                select(
                    ArticleAuthor.author_name,
                    ArticleAuthor.author_order,
                    Author.slug,
                )
                .outerjoin(Author, Author.id == ArticleAuthor.author_id)
                .where(ArticleAuthor.article_id == article_id)
                .order_by(ArticleAuthor.author_order)
            )
        ).all()
        return [
            {"name": r.author_name, "slug": r.slug, "order": r.author_order}
            for r in rows
        ]

    async def claim(
        self, db: AsyncSession, slug: str, profile_id: str
    ) -> dict[str, Any]:
        """Присвоить карточку себе.

        Проверки намеренно минимальны (нужен только вход): ложное присвоение
        карточки однофамильца обратимо — владелец платформы отвяжет, — а барьер
        из документов на этом шаге убил бы весь смысл затеи. Уже присвоенную
        карточку второй раз забрать нельзя.
        """
        row = (
            await db.execute(select(Author).where(Author.slug == slug))
        ).scalar_one_or_none()
        if row is None:
            raise AuthorCardError("Author card not found")
        uid = uuid.UUID(str(profile_id))
        if row.profile_id is not None and row.profile_id != uid:
            raise AuthorCardError("Author card already claimed")

        row.profile_id = uid
        # Дублируем связь в подписи: страницы профиля (/researcher/u/<id>)
        # собирают публикации по article_authors.profile_id, и без этого шага
        # присвоенная карточка не появилась бы в самом профиле.
        await db.execute(
            ArticleAuthor.__table__.update()
            .where(ArticleAuthor.author_id == row.id)
            .values(profile_id=uid)
        )
        await db.commit()
        return {"slug": row.slug, "profile_id": str(uid), "works": row.works_count}

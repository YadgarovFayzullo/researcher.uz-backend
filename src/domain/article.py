"""Статьи, standalone-публикации и доклады конференций (`articles`)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

from slugify import slugify
from sqlalchemy import Integer, Select, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.serialization import HEAVY_ARTICLE_COLUMNS, row_to_dict
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    ArticleInteraction,
    ArticleReference,
    Issue,
    Journal,
    Publisher,
    SavedArticle,
)
from src.schemas.article import ArticleCreate, ArticleUpdate

# Вектор эмбеддинга наружу не отдаём и не тянем из БД: это сотни чисел на
# строку, на списке в тысячу статей — мегабайты впустую.
_HEAVY = HEAVY_ARTICLE_COLUMNS
# Для списков дополнительно режем тяжёлый текст, который в каталогах не виден.
_LIST_EXCLUDE = _HEAVY + ("annotation_foreign", "keywords_foreign")

_SORTABLE = {
    "created_at": Article.created_at,
    "data": Article.data,
    "title": Article.title,
    "publication_year": Article.publication_year,
    "id": Article.id,
}


# Узбекская кириллица: python-slugify транслитерирует её по русским правилам
# (Қ→k, Ў→u, Ҳ→kh), и «ҚАНДЛИ ЎЗГАРИШЛАРИ» превращалось в «kandli-uzgarishlari».
# Подменяем эти буквы до slugify, чтобы получалось «qandli-ozgarishlari».
# Апострофы у ў/ғ опускаем: в слуге они всё равно недопустимы.
_UZ_CYRILLIC = str.maketrans(
    {
        "қ": "q", "Қ": "Q",
        "ў": "o", "Ў": "O",
        "ғ": "g", "Ғ": "G",
        "ҳ": "h", "Ҳ": "H",
    }
)


class ArticleDomain:
    def validate_publication(self, title: str) -> bool:
        return bool(title and len(title) > 5)

    def generate_slug(self, text: str) -> str:
        """Текст (в т.ч. кириллицу) → URL-friendly slug.
        'Древняя Греция' -> 'drevniaia-gretsiia'"""
        return slugify((text or "").translate(_UZ_CYRILLIC))

    def get_current_time(self) -> datetime:
        return datetime.now(timezone.utc)

    # ------------------------------------------------------------- чтение

    def _apply_filters(
        self,
        stmt: Select[Any],
        *,
        issue_id: Sequence[int] | None,
        journal_id: Sequence[int] | None,
        publisher_id: int | None,
        admin_id: str | None,
        section_id: int | None,
        publication_type: Sequence[str] | None,
        field_of_science: Sequence[str] | None,
        published: bool | None,
        has_doi: bool | None,
        has_issue: bool | None,
        created_after: datetime | None,
        q: str | None = None,
    ) -> Select[Any]:
        if q:
            # Подстрочный поиск каталога: название (обе локали) ИЛИ автор. Это не
            # ранжированный полнотекст (для него есть /search), а фильтр «содержит»
            # внутри уже суженной выборки (журнал/выпуск) — там строк немного.
            like = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    Article.title.ilike(like),
                    Article.title_foreign.ilike(like),
                    Article.authors.ilike(like),
                )
            )
        if issue_id:
            stmt = stmt.where(Article.issue_id.in_(issue_id))
        if journal_id:
            # Через выпуск: статей напрямую к журналу не привязано. Список, а не
            # одно значение: у админа бывает несколько журналов, и собирать по
            # ним id выпусков отдельными запросами было бы N+1.
            stmt = stmt.where(
                Article.issue_id.in_(
                    select(Issue.id).where(Issue.journal_id.in_(journal_id))
                )
            )
        if publisher_id is not None:
            stmt = stmt.where(Article.publisher_id == publisher_id)
        if admin_id is not None:
            stmt = stmt.where(Article.admin_id == admin_id)
        if section_id is not None:
            stmt = stmt.where(Article.section_id == section_id)
        if publication_type:
            stmt = stmt.where(Article.publication_type.in_(publication_type))
        if field_of_science:
            stmt = stmt.where(Article.field_of_science.in_(field_of_science))
        if published is not None:
            stmt = stmt.where(Article.published.is_(published))
        if has_doi is not None:
            stmt = stmt.where(
                Article.doi.isnot(None) if has_doi else Article.doi.is_(None)
            )
        if created_after is not None:
            stmt = stmt.where(Article.created_at >= created_after)
        if has_issue is not None:
            # Отделяет статьи выпусков от самостоятельных изданий издательства
            # (у тех issue_id пуст). Замена inner join'а на issues во фронте.
            stmt = stmt.where(
                Article.issue_id.isnot(None) if has_issue else Article.issue_id.is_(None)
            )
        return stmt

    async def list_articles(
        self,
        db: AsyncSession,
        *,
        issue_id: Sequence[int] | None = None,
        journal_id: Sequence[int] | None = None,
        publisher_id: int | None = None,
        admin_id: str | None = None,
        section_id: int | None = None,
        publication_type: Sequence[str] | None = None,
        field_of_science: Sequence[str] | None = None,
        published: bool | None = None,
        has_doi: bool | None = None,
        has_issue: bool | None = None,
        created_after: datetime | None = None,
        q: str | None = None,
        order_by: str = "created_at",
        descending: bool = True,
        limit: int | None = None,
        offset: int = 0,
        with_stats: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """Срез статей + общее число подходящих строк.

        Возвращает total отдельно, потому что фронту нужна пагинация: без него
        каталог вынужден тянуть всю выборку, чтобы узнать её размер.
        """
        filters = dict(
            issue_id=issue_id,
            journal_id=journal_id,
            publisher_id=publisher_id,
            admin_id=admin_id,
            section_id=section_id,
            publication_type=publication_type,
            field_of_science=field_of_science,
            published=published,
            has_doi=has_doi,
            has_issue=has_issue,
            created_after=created_after,
            q=q,
        )

        total = (
            await db.execute(
                self._apply_filters(select(func.count(Article.id)), **filters)
            )
        ).scalar_one()

        # views/downloads берём из денормализованных колонок статьи — без join и
        # group by по article_interactions. Прежний агрегат сканировал весь
        # журнал независимо от LIMIT (аггрегация до лимита); теперь это обычные
        # колонки, сортировка по ним доступна всегда.
        cols: list[Any] = [
            Article,
            Journal.name.label("journal_name"),
            Journal.slug.label("journal_slug"),
            Publisher.name.label("publisher_name"),
        ]

        stmt = (
            select(*cols)
            .outerjoin(Issue, Issue.id == Article.issue_id)
            .outerjoin(Journal, Journal.id == Issue.journal_id)
            .outerjoin(Publisher, Publisher.id == Article.publisher_id)
        )
        stmt = self._apply_filters(stmt, **filters)

        # "pages" — по первому числу диапазона ("12-20"→12); substring по regex
        # даёт NULL без ведущих цифр (уезжает в конец). Остальные — из _SORTABLE.
        if order_by == "views":
            sort_col = Article.views_count
        elif order_by == "downloads":
            sort_col = Article.downloads_count
        elif order_by == "pages":
            # Первая группа цифр в строке страниц (" 235–240" → 235). Без якоря
            # ^, т.к. значения бывают с ведущим пробелом/буквами; NULL (нет цифр)
            # уезжает в конец. Паритет с клиентским firstPage().
            sort_col = cast(func.substring(Article.pages, r"\d+"), Integer)
        else:
            sort_col = _SORTABLE.get(order_by, Article.created_at)
        stmt = stmt.order_by(
            sort_col.desc().nullslast() if descending else sort_col.asc().nullsfirst(),
            Article.id.desc(),
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        if offset:
            stmt = stmt.offset(offset)

        rows = (await db.execute(stmt)).all()
        items: list[dict[str, Any]] = []
        for row in rows:
            # В списках выкидываем иностранную аннотацию/ключевые слова: каталоги
            # их не показывают, а annotation_foreign бывает до 4 КБ на строку —
            # на журнале в ~2000 статей это мегабайты лишнего трафика. Полный
            # текст статьи отдаёт деталка /article/<slug>, не этот эндпоинт.
            data = row_to_dict(row.Article, Article, exclude=_LIST_EXCLUDE)
            data["journal_name"] = row.journal_name
            data["journal_slug"] = row.journal_slug
            data["publisher_name"] = row.publisher_name
            if with_stats:
                data["views"] = int(row.Article.views_count or 0)
                data["downloads"] = int(row.Article.downloads_count or 0)
            items.append(data)
        return items, int(total)

    async def count_articles(self, db: AsyncSession, **filters: Any) -> int:
        stmt = self._apply_filters(select(func.count(Article.id)), **filters)
        return int((await db.execute(stmt)).scalar_one())

    async def journal_facets(
        self, db: AsyncSession, journal_id: int
    ) -> dict[str, Any]:
        """Лёгкие агрегаты журнала одним махом — вместо выкачки всех статей ради
        сайдбара/шапки/наукометрии. Возвращает:

        - article_ids  — ВСЕ id статей журнала (вход дашборда цитирований);
        - issue_counts — {issue_id: число статей} для счётчиков дерева выпусков;
        - field_counts — [{field, count}] для фасета направлений науки;
        - total_views / total_downloads — суммарные метрики журнала (шапка).
        """
        issue_ids = select(Issue.id).where(Issue.journal_id == journal_id)

        rows = (
            await db.execute(
                select(
                    Article.id,
                    Article.issue_id,
                    Article.views_count,
                    Article.downloads_count,
                ).where(Article.issue_id.in_(issue_ids))
            )
        ).all()
        article_ids = [r.id for r in rows]
        issue_counts: dict[int, int] = {}
        total_views = 0
        total_downloads = 0
        for r in rows:
            if r.issue_id is not None:
                issue_counts[r.issue_id] = issue_counts.get(r.issue_id, 0) + 1
            total_views += r.views_count or 0
            total_downloads += r.downloads_count or 0

        field_rows = (
            await db.execute(
                select(Article.field_of_science, func.count(Article.id))
                .where(
                    Article.issue_id.in_(issue_ids),
                    Article.field_of_science.isnot(None),
                )
                .group_by(Article.field_of_science)
                .order_by(func.count(Article.id).desc())
            )
        ).all()
        field_counts = [{"field": f, "count": int(c)} for f, c in field_rows]

        # total_views/downloads суммируем из уже выбранных колонок статей —
        # отдельного скана article_interactions больше нет.
        return {
            "article_ids": article_ids,
            "issue_counts": issue_counts,
            "field_counts": field_counts,
            "total_views": int(total_views),
            "total_downloads": int(total_downloads),
        }

    async def fields_of_science(self, db: AsyncSession) -> list[dict[str, Any]]:
        """{field, count} — фасет для FieldsOfScienceSection. Считаем в БД, а не
        выгрузкой всех статей ради группировки на клиенте."""
        res = await db.execute(
            select(Article.field_of_science, func.count(Article.id))
            .where(Article.field_of_science.isnot(None))
            .group_by(Article.field_of_science)
            .order_by(func.count(Article.id).desc())
        )
        return [{"field": f, "count": int(c)} for f, c in res.all()]

    async def lookup(
        self,
        db: AsyncSession,
        *,
        slugs: Sequence[str] | None = None,
        ids: Sequence[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Батч-разрешение слугов/id в {id, title, slug}.

        Нужен редактору списка литературы: строку библиографии матчим на статью
        платформы по ссылке /article/<slug> или по внутреннему id (RUZ-632).
        Один запрос на весь список вместо запроса на строку.
        """
        conds = []
        if slugs:
            conds.append(Article.slug.in_(slugs))
        if ids:
            conds.append(Article.id.in_(ids))
        if not conds:
            return []
        res = await db.execute(
            select(Article.id, Article.title, Article.slug).where(or_(*conds))
        )
        return [{"id": r.id, "title": r.title, "slug": r.slug} for r in res.all()]

    async def get_article_by_slug(self, db: AsyncSession, slug: str) -> Article | None:
        result = await db.execute(select(Article).where(Article.slug == slug))
        return result.scalars().first()

    async def get_article_by_id(self, db: AsyncSession, id: int) -> Article | None:
        result = await db.execute(select(Article).where(Article.id == id))
        return result.scalars().first()

    # ------------------------------------------------------------- запись

    async def _unique_slug(self, db: AsyncSession, title: str) -> str:
        slug = self.generate_slug(title or "")
        if not slug:
            slug = str(uuid.uuid4())[:8]
        if await self.get_article_by_slug(db, slug):
            slug = f"{slug}-{str(uuid.uuid4())[:6]}"
        return slug

    async def create_article(
        self, db: AsyncSession, article_in: ArticleCreate
    ) -> Article:
        # exclude_unset: не подставляем None в колонки, которых не было в теле,
        # чтобы работали server_default (publication_type, published, metadata).
        data = article_in.model_dump(exclude_unset=True, by_alias=False)
        # Слуг берём из тела, если прислали, иначе выводим из заголовка. В обоих
        # случаях прогоняем через _unique_slug: предложенный клиентом слуг тоже
        # может оказаться занятым.
        proposed = data.pop("slug", None)
        new_article = Article(
            **data,
            slug=await self._unique_slug(db, proposed or article_in.title),
            created_at=self.get_current_time(),
        )
        db.add(new_article)
        await db.commit()
        await db.refresh(new_article)
        return new_article

    async def update_article(
        self, db: AsyncSession, id: int, article_in: ArticleUpdate
    ) -> Article | None:
        article = await self.get_article_by_id(db, id)
        if not article:
            return None
        for field, value in article_in.model_dump(
            exclude_unset=True, by_alias=False
        ).items():
            setattr(article, field, value)
        await db.commit()
        await db.refresh(article)
        return article

    async def delete_article(self, db: AsyncSession, id: int) -> bool:
        article = await self.get_article_by_id(db, id)
        if not article:
            return False
        # Зависимые строки сносим явно: ON DELETE CASCADE в схеме нет, без
        # этого удаление падает на FK. Ссылки чистим с обеих сторон — статья
        # может и цитировать, и быть процитированной.
        await db.execute(
            ArticleAuthor.__table__.delete().where(ArticleAuthor.article_id == id)
        )
        await db.execute(
            ArticleReference.__table__.delete().where(
                ArticleReference.article_id == id
            )
        )
        await db.execute(
            ArticleReference.__table__.update()
            .where(ArticleReference.cited_article_id == id)
            .values(cited_article_id=None)
        )
        await db.execute(
            SavedArticle.__table__.delete().where(SavedArticle.article_id == id)
        )
        await db.execute(
            ArticleInteraction.__table__.delete().where(
                ArticleInteraction.article_id == id
            )
        )
        await db.delete(article)
        await db.commit()
        return True

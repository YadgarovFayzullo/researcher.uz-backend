"""Цитирования — порт `supabase/citations.sql` (RPC → сервис).

Гибридная модель:
  • external_citations  — кэш мировых счётчиков из OpenAlex по DOI.
  • article_references  — внутренний граф (кто кого цитирует).
«Cited by» = GREATEST(внешний, внутренний), чтобы не двоить.

Порт RPC:
  get_article_citations / get_citing_articles / match_articles_by_doi (public, SECURITY DEFINER)
  upsert_external_citations (owner-only — проверка роли внутри, как в SQL).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import aliased
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.persistence.models import (
    Article,
    ArticleReference,
    ExternalCitation,
)

# ^https?://(dx.)?doi.org/ — нормализация DOI (как regexp_replace в SQL).
_DOI_PREFIX = re.compile(r"^https?://(dx\.)?doi\.org/", re.IGNORECASE)


def _norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    return _DOI_PREFIX.sub("", doi).lower()


def _pick_year(pub_year: Any, data_str: Any, created_at: Any) -> int | None:
    """Год работы: publication_year → data → created_at, первый > 1900."""
    try:
        if pub_year and int(pub_year) > 1900:
            return int(pub_year)
    except (TypeError, ValueError):
        pass
    for value in (data_str, created_at):
        if not value:
            continue
        year = getattr(value, "year", None)
        if year is None:
            try:
                year = datetime.fromisoformat(str(value)[:19]).year
            except ValueError:
                continue
        if year and year > 1900:
            return int(year)
    return None


class NotOwner(Exception):
    """upsert_external_citations вызван не-owner (SQL: raise 'forbidden: owner only')."""


class CitationsDomain:
    async def get_citation_years(
        self, db: AsyncSession, article_ids: list[int]
    ) -> list[dict]:
        """Цитирования по годам, просуммированные по набору статей.

        Один эндпоинт на два случая: график на странице статьи (один id) и на
        профиле исследователя (все его работы) — раньше это были два разных
        запроса к таблице, теперь суммирование делает сервер.
        """
        clean = [int(i) for i in dict.fromkeys(article_ids)]
        if not clean:
            return []
        rows = (
            await db.execute(
                select(ExternalCitation.counts_by_year).where(
                    ExternalCitation.article_id.in_(clean)
                )
            )
        ).all()

        by_year: dict[int, int] = {}
        for (counts,) in rows:
            for entry in counts or []:
                try:
                    year = int(entry["year"])
                except (KeyError, TypeError, ValueError):
                    continue
                by_year[year] = by_year.get(year, 0) + int(entry.get("count") or 0)
        return [
            {"year": y, "count": c} for y, c in sorted(by_year.items())
        ]

    async def get_citation_breakdown(
        self, db: AsyncSession, article_ids: list[int]
    ) -> dict:
        """Цитирования по годам В РАЗРЕЗЕ статьи — для дашборда наукометрии.

        Отличается от get_citation_years тем, что не схлопывает всё в один ряд:
        дашборду нужно и «всего по годам», и окно «за последние N лет» по каждой
        работе, а из суммы по всем статьям второе не восстановить.

        Год цитирующей работы берём как publication_year → data → created_at
        (первый разумный, > 1900) — тот же порядок, что и на карточке статьи.
        """
        clean = [int(i) for i in dict.fromkeys(article_ids)]
        if not clean:
            return {"external": [], "internal": []}

        ext_rows = (
            await db.execute(
                select(
                    ExternalCitation.article_id, ExternalCitation.counts_by_year
                ).where(ExternalCitation.article_id.in_(clean))
            )
        ).all()
        external: list[dict] = []
        for aid, counts in ext_rows:
            for entry in counts or []:
                try:
                    year = int(entry["year"])
                except (KeyError, TypeError, ValueError):
                    continue
                if year <= 1900:
                    continue
                external.append(
                    {
                        "article_id": int(aid),
                        "year": year,
                        "count": int(entry.get("count") or 0),
                    }
                )

        citing = aliased(Article)
        int_rows = (
            await db.execute(
                select(
                    ArticleReference.cited_article_id,
                    citing.publication_year,
                    citing.data,
                    citing.created_at,
                )
                .join(citing, citing.id == ArticleReference.article_id)
                .where(ArticleReference.cited_article_id.in_(clean))
            )
        ).all()

        internal: list[dict] = []
        for cited_id, pub_year, data_str, created_at in int_rows:
            year = _pick_year(pub_year, data_str, created_at)
            if year is None:
                continue
            internal.append(
                {"cited_article_id": int(cited_id), "year": year, "count": 1}
            )

        return {"external": external, "internal": internal}

    async def get_article_citations(
        self, db: AsyncSession, article_ids: list[int]
    ) -> list[dict]:
        """Батч-счётчик цитирований: internal / external / cited_by (=GREATEST).

        Порт get_article_citations(bigint[]). Возвращает строку на каждый
        запрошенный id (даже с нулями), сохраняя порядок входа.
        """
        if not article_ids:
            return []

        # Внешние счётчики (OpenAlex-кэш).
        ext_rows = (
            await db.execute(
                select(
                    ExternalCitation.article_id, ExternalCitation.cited_by_count
                ).where(ExternalCitation.article_id.in_(article_ids))
            )
        ).all()
        ext = {aid: int(c or 0) for aid, c in ext_rows}

        # Внутренние рёбра: сколько раз статья цитируется в article_references.
        intn_rows = (
            await db.execute(
                select(
                    ArticleReference.cited_article_id,
                    func.count().label("c"),
                )
                .where(ArticleReference.cited_article_id.in_(article_ids))
                .group_by(ArticleReference.cited_article_id)
            )
        ).all()
        intn = {aid: int(c) for aid, c in intn_rows}

        out: list[dict] = []
        for aid in article_ids:
            internal = intn.get(aid, 0)
            external = ext.get(aid, 0)
            out.append(
                {
                    "article_id": aid,
                    "internal_count": internal,
                    "external_count": external,
                    "cited_by": max(internal, external),
                }
            )
        return out

    async def get_citing_articles(
        self, db: AsyncSession, article_id: int
    ) -> list[dict]:
        """Статьи платформы, цитирующие данную (внутренний граф).

        Порт get_citing_articles(bigint): distinct статьи-источники,
        order by created_at desc nulls last.
        """
        rows = (
            await db.execute(
                select(
                    Article.id,
                    Article.title,
                    Article.slug,
                    Article.authors,
                    Article.publication_year,
                    Article.created_at,
                )
                .join(
                    ArticleReference,
                    ArticleReference.article_id == Article.id,
                )
                .where(ArticleReference.cited_article_id == article_id)
                .distinct()
                .order_by(Article.created_at.desc().nullslast())
            )
        ).all()
        return [
            {
                "id": r.id,
                "title": r.title,
                "slug": r.slug,
                "authors": r.authors,
                "publication_year": r.publication_year,
                "created_at": r.created_at,
            }
            for r in rows
        ]

    async def match_articles_by_doi(
        self, db: AsyncSession, dois: list[str]
    ) -> list[dict]:
        """Матчинг DOI списка литературы на статьи платформы.

        Порт match_articles_by_doi(text[]). Клиент передаёт нормализованные
        DOI; здесь нормализуем сторону articles.doi и сравниваем.
        """
        if not dois:
            return []
        wanted = {d for d in (_norm_doi(x) for x in dois) if d}
        if not wanted:
            return []

        rows = (
            await db.execute(
                select(Article.id, Article.title, Article.slug, Article.doi).where(
                    Article.doi.isnot(None), Article.doi != ""
                )
            )
        ).all()
        out: list[dict] = []
        for r in rows:
            norm = _norm_doi(r.doi)
            if norm in wanted:
                out.append(
                    {"norm_doi": norm, "id": r.id, "title": r.title, "slug": r.slug}
                )
        return out

    async def upsert_external_citations(
        self, db: AsyncSession, *, caller_is_owner: bool, rows: list[dict]
    ) -> int:
        """Upsert кэша внешних цитирований (owner-only).

        Порт upsert_external_citations(jsonb): в SQL роль проверяется внутри
        функции (raise 'forbidden: owner only'); здесь — параметр caller_is_owner
        (defense-in-depth дополнительно к require_owner в эндпоинте).
        Возвращает число обработанных строк.
        """
        if not caller_is_owner:
            raise NotOwner("forbidden: owner only")
        if not rows:
            return 0

        n = 0
        for r in rows:
            aid = r.get("article_id")
            if aid is None:
                continue
            values = {
                "article_id": int(aid),
                "doi": (r.get("doi") or None) or None,
                "cited_by_count": int(r.get("cited_by_count") or 0),
                "counts_by_year": r.get("counts_by_year") or [],
                "source": (r.get("source") or "").strip() or "openalex",
                "updated_at": func.now(),
            }
            stmt = (
                pg_insert(ExternalCitation)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[ExternalCitation.article_id],
                    set_={
                        "doi": values["doi"],
                        "cited_by_count": values["cited_by_count"],
                        "counts_by_year": values["counts_by_year"],
                        "source": values["source"],
                        "updated_at": func.now(),
                    },
                )
            )
            await db.execute(stmt)
            n += 1

        await db.commit()
        return n

    async def refresh_from_openalex(
        self,
        db: AsyncSession,
        *,
        caller_is_owner: bool,
        limit: int = 500,
        offset: int = 0,
    ) -> dict:
        """Обновить кэш external_citations из OpenAlex (owner-only).

        Порт owner-роута /api/citations/refresh: страница статей с DOI (order by id,
        limit/offset) → OpenAlex → upsert (в т.ч. нулями). Возвращает счётчики
        для пагинации по всей базе несколькими вызовами.
        """
        if not caller_is_owner:
            raise NotOwner("forbidden: owner only")

        # локальный импорт — не тащить httpx в модуль, если refresh не зовут
        from src.infrastructure.external.openalex import (
            fetch_citations_by_dois,
            normalize_doi,
        )

        limit = min(max(int(limit or 500), 1), 2000)
        offset = max(int(offset or 0), 0)

        rows = (
            await db.execute(
                select(Article.id, Article.doi)
                .where(Article.doi.isnot(None), Article.doi != "")
                .order_by(Article.id.asc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        with_doi = [(aid, doi) for aid, doi in rows if doi]

        # DOI → article_id (при дублях DOI берём последнюю статью, как в TS).
        doi_to_id: dict[str, int] = {}
        for aid, doi in with_doi:
            doi_to_id[normalize_doi(doi)] = aid

        cites = await fetch_citations_by_dois([d for _, d in with_doi])

        upsert_rows = []
        for doi, aid in doi_to_id.items():
            c = cites.get(doi)
            upsert_rows.append(
                {
                    "article_id": aid,
                    "doi": doi,
                    "cited_by_count": (c or {}).get("cited_by_count", 0),
                    "counts_by_year": (c or {}).get("counts_by_year", []),
                    "source": "openalex",
                }
            )

        updated = 0
        if upsert_rows:
            updated = await self.upsert_external_citations(
                db, caller_is_owner=True, rows=upsert_rows
            )

        matched = sum(1 for d in doi_to_id if d in cites)
        return {
            "processed": len(with_doi),
            "matched": matched,
            "updated": updated,
            "offset": offset,
            "nextOffset": offset + len(with_doi),
        }

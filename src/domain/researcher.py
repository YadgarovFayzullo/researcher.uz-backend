"""Кабинет исследователя — порт `supabase/researcher_cabinet.sql` + get_researcher_profile.

Все RPC в SQL — SECURITY DEFINER, переопределяют личность через auth.uid()
и привязанный orcid_id, поэтому пользователь может действовать только над своим
профилем / своими работами. Здесь личность приходит из JWT (get_current_profile),
а сервис принимает user_id и сам подтягивает orcid из profiles.
"""
from __future__ import annotations

from typing import Any

import re

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    Profile,
    ResearcherWork,
)

_DOI_PREFIX = re.compile(r"^https?://(dx\.)?doi\.org/", re.IGNORECASE)


def _norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    return _DOI_PREFIX.sub("", doi.strip()).lower() or None


class CabinetError(Exception):
    """Ошибка бизнес-правила кабинета (нет ORCID, статья не найдена и т.п.)."""


def _nullif_empty(value: str | None) -> str | None:
    """SQL nullif(x, '') — пустую строку превращаем в None."""
    if value is None:
        return None
    return value or None


class ResearcherDomain:
    async def public_profiles(
        self, db: AsyncSession, limit: int = 12
    ) -> list[dict[str, Any]]:
        """Публичные профили с аватаром — для слайдера на главной.

        Отдаём только то, что и так видно на публичной карточке исследователя:
        имя, аватар, место работы, должность, страна. Ни email, ни роли здесь
        быть не должно — это анонимный эндпоинт.
        """
        rows = (
            await db.execute(
                select(
                    Profile.id,
                    Profile.full_name,
                    Profile.avatar_url,
                    Profile.workplace,
                    Profile.position,
                    Profile.country,
                    Profile.orcid_id,
                    Profile.created_at,
                )
                .where(
                    Profile.is_public.is_(True),
                    Profile.avatar_url.isnot(None),
                    Profile.avatar_url != "",
                )
                .order_by(Profile.created_at.desc().nullslast())
                .limit(max(1, min(limit, 50)))
            )
        ).all()
        return [
            {
                "id": str(r.id),
                "full_name": r.full_name,
                "avatar_url": r.avatar_url,
                "workplace": r.workplace,
                "position": r.position,
                "country": r.country,
                "orcid_id": r.orcid_id,
                "created_at": r.created_at,
            }
            for r in rows
        ]

    async def _profile(self, db: AsyncSession, user_id) -> Profile:
        prof = (
            await db.execute(select(Profile).where(Profile.id == user_id))
        ).scalars().first()
        if prof is None:
            raise CabinetError("profile not found")
        return prof

    # ------------------------------------------------------------------ #
    async def update_my_profile(
        self,
        db: AsyncSession,
        *,
        user_id,
        full_name: str | None = None,
        workplace: str | None = None,
        country: str | None = None,
        education: str | None = None,
        bio: str | None = None,
        avatar_url: str | None = None,
    ) -> None:
        """Порт update_my_profile: NULL = не трогать, '' = очистить поле."""
        prof = await self._profile(db, user_id)
        # case when p_x is null then x else nullif(p_x, '') end
        if full_name is not None:
            prof.full_name = _nullif_empty(full_name)
        if workplace is not None:
            prof.workplace = _nullif_empty(workplace)
        if country is not None:
            prof.country = _nullif_empty(country)
        if education is not None:
            prof.education = _nullif_empty(education)
        if bio is not None:
            prof.bio = _nullif_empty(bio)
        if avatar_url is not None:
            prof.avatar_url = _nullif_empty(avatar_url)
        prof.updated_at = func.now()
        await db.commit()

    # ------------------------------------------------------------------ #
    async def _orcid_and_name(self, db: AsyncSession, user_id) -> tuple[str, str]:
        prof = await self._profile(db, user_id)
        if not prof.orcid_id:
            raise CabinetError("no ORCID linked")
        name = (prof.full_name or "").strip() or "Автор"
        return prof.orcid_id, name

    async def _next_author_order(self, db: AsyncSession, article_id: int) -> int:
        # coalesce(max(author_order) + 1, 0)
        mx = (
            await db.execute(
                select(func.max(ArticleAuthor.author_order)).where(
                    ArticleAuthor.article_id == article_id
                )
            )
        ).scalar()
        return (mx + 1) if mx is not None else 0

    async def claim_article(self, db: AsyncSession, *, user_id, article_id: int) -> None:
        """Порт claim_article: привязать статью к профилю (idempotent)."""
        v_orcid, v_name = await self._orcid_and_name(db, user_id)

        exists_article = (
            await db.execute(select(Article.id).where(Article.id == article_id))
        ).scalars().first()
        if exists_article is None:
            raise CabinetError("article not found")

        # Уже на этом профиле? (по profile_id или orcid)
        already = (
            await db.execute(
                select(ArticleAuthor.id).where(
                    ArticleAuthor.article_id == article_id,
                    (ArticleAuthor.profile_id == user_id)
                    | (ArticleAuthor.orcid == v_orcid),
                )
            )
        ).scalars().first()
        if already is not None:
            return

        order = await self._next_author_order(db, article_id)
        db.add(
            ArticleAuthor(
                article_id=article_id,
                author_order=order,
                author_name=v_name,
                orcid=v_orcid,
                profile_id=user_id,
                is_verified=True,
            )
        )
        await db.commit()

    async def unclaim_article(
        self, db: AsyncSession, *, user_id, article_id: int
    ) -> None:
        """Порт unclaim_article: удалить только свои claim-строки."""
        # auth.uid() null check обеспечивает get_current_profile.
        await db.execute(
            ArticleAuthor.__table__.delete().where(
                ArticleAuthor.article_id == article_id,
                ArticleAuthor.profile_id == user_id,
            )
        )
        await db.commit()

    async def claim_articles_by_dois(
        self, db: AsyncSession, *, user_id, dois: list[str]
    ) -> int:
        """Порт claim_articles_by_dois: bulk-привязка по DOI. Возвращает число новых."""
        v_orcid, v_name = await self._orcid_and_name(db, user_id)
        wanted = {d for d in (_norm_doi(x) for x in (dois or [])) if d}
        if not wanted:
            return 0

        # Кандидаты: статьи с DOI, чей нормализованный DOI в наборе, ещё не
        # привязанные к этому профилю/ORCID.
        rows = (
            await db.execute(
                select(Article.id, Article.doi).where(
                    Article.doi.isnot(None), Article.doi != ""
                )
            )
        ).all()

        count = 0
        for aid, doi in rows:
            if _norm_doi(doi) not in wanted:
                continue
            already = (
                await db.execute(
                    select(ArticleAuthor.id).where(
                        ArticleAuthor.article_id == aid,
                        (ArticleAuthor.profile_id == user_id)
                        | (ArticleAuthor.orcid == v_orcid),
                    )
                )
            ).scalars().first()
            if already is not None:
                continue
            order = await self._next_author_order(db, aid)
            db.add(
                ArticleAuthor(
                    article_id=aid,
                    author_order=order,
                    author_name=v_name,
                    orcid=v_orcid,
                    profile_id=user_id,
                    is_verified=True,
                )
            )
            count += 1

        await db.commit()
        return count

    async def import_orcid_works(
        self, db: AsyncSession, *, user_id, works: list[dict]
    ) -> int:
        """Порт import_orcid_works: полный replace внешних работ (ORCID = источник истины).

        Возвращает число сохранённых работ.
        """
        prof = await self._profile(db, user_id)
        if not prof.orcid_id:
            raise CabinetError("no ORCID linked")
        v_orcid = prof.orcid_id

        # delete all, потом вставляем актуальные (works removed in ORCID disappear).
        await db.execute(
            ResearcherWork.__table__.delete().where(ResearcherWork.orcid == v_orcid)
        )

        seen: set[str] = set()
        for w in works or []:
            put_code = (w.get("put_code") or "").strip()
            if not put_code or put_code in seen:  # on conflict (orcid, put_code) do nothing
                continue
            seen.add(put_code)
            year = w.get("year")
            try:
                year_val = int(year) if year not in (None, "") else None
            except (TypeError, ValueError):
                year_val = None
            db.add(
                ResearcherWork(
                    orcid=v_orcid,
                    put_code=put_code,
                    title=_nullif_empty(w.get("title")),
                    work_type=_nullif_empty(w.get("work_type")),
                    year=year_val,
                    doi=_nullif_empty(w.get("doi")),
                    url=_nullif_empty(w.get("url")),
                    container=_nullif_empty(w.get("container")),
                )
            )

        await db.commit()
        n = (
            await db.execute(
                select(func.count())
                .select_from(ResearcherWork)
                .where(ResearcherWork.orcid == v_orcid)
            )
        ).scalar()
        return int(n or 0)

    async def get_researcher_profile(
        self, db: AsyncSession, orcid: str
    ) -> dict | None:
        """Порт get_researcher_profile: публичная карточка по ORCID (без id/role/email)."""
        row = (
            await db.execute(
                select(
                    Profile.full_name,
                    Profile.orcid_id,
                    Profile.avatar_url,
                    Profile.workplace,
                    Profile.country,
                    Profile.bio,
                    Profile.education,
                ).where(Profile.orcid_id == orcid)
            )
        ).first()
        if row is None:
            return None
        return {
            "full_name": row.full_name,
            "orcid": row.orcid_id,
            "avatar_url": row.avatar_url,
            "workplace": row.workplace,
            "country": row.country,
            "bio": row.bio,
            "education": row.education,
        }

    async def list_researcher_works(
        self, db: AsyncSession, orcid: str
    ) -> list[ResearcherWork]:
        """Внешние работы исследователя (researcher_works, public read)."""
        rows = (
            await db.execute(
                select(ResearcherWork)
                .where(ResearcherWork.orcid == orcid)
                .order_by(ResearcherWork.year.desc().nullslast())
            )
        ).scalars().all()
        return list(rows)

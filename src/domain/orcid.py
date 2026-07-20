"""ORCID — привязка к профилю (порт RPC `link_my_orcid`).

Импорт работ (`import_orcid_works`) и публичный профиль исследователя — Фаза 5.
Здесь только то, что нужно OAuth-callback'у: записать orcid+данные в profile и
«застолбить» (claim) все строки article_authors с этим ORCID за текущего юзера.
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.persistence.models import ArticleAuthor, Profile


class OrcidTaken(Exception):
    """ORCID уже привязан к другому аккаунту."""


class OrcidDomain:
    async def link_orcid(
        self,
        db: AsyncSession,
        *,
        user_id,
        orcid: str,
        full_name: str = "",
        workplace: str = "",
        country: str = "",
        bio: str = "",
        education: str = "",
    ) -> None:
        # ORCID не должен принадлежать другому профилю (аналог проверки в RPC).
        res = await db.execute(
            select(Profile).where(Profile.orcid_id == orcid, Profile.id != user_id)
        )
        if res.scalars().first():
            raise OrcidTaken()

        profile = (
            await db.execute(select(Profile).where(Profile.id == user_id))
        ).scalars().first()
        if profile is None:
            profile = Profile(id=user_id, role="authenticated")
            db.add(profile)

        profile.orcid_id = orcid
        # непустые поля перезаписываем (как link_my_orcid)
        if full_name:
            profile.full_name = full_name
        if workplace:
            profile.workplace = workplace
        if country:
            profile.country = country
        if bio:
            profile.bio = bio
        if education:
            profile.education = education

        # claim: привязать все article_authors с этим ORCID к профилю юзера
        await db.execute(
            update(ArticleAuthor)
            .where(ArticleAuthor.orcid == orcid)
            .values(profile_id=user_id, is_verified=True)
        )
        await db.commit()

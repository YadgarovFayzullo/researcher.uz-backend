"""Кабинет исследователя — эндпоинты (Фаза 5, порт researcher_cabinet.sql).

Публичное: GET /researcher/{orcid} (карточка + внешние работы) и то же самое по
id аккаунта — GET /researcher/u/{user_id} — для профилей без ORCID (регистрация
через Google).
Под сессией: обновить профиль, claim/unclaim статей, импорт работ ORCID.
Личность берётся из get_current_profile (JWT), сервис действует только над своим
профилем — как SECURITY DEFINER + auth.uid() в SQL.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_profile
from src.domain.content import AuthorDomain
from src.domain.researcher import CabinetError, ResearcherDomain
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Profile
from src.schemas.researcher import (
    DoisRequest,
    ImportWorksRequest,
    ResearcherPageResponse,
    UpdateMyProfileRequest,
)

router = APIRouter()
domain = ResearcherDomain()
_authors = AuthorDomain()


# Объявлен ДО /{orcid}: иначе "public-profiles" уедет в параметр orcid.
@router.get("/public-profiles")
async def public_profiles(limit: int = 12, db: AsyncSession = Depends(get_db)):
    """Публичные профили с аватаром (слайдер на главной). Аноним."""
    return await domain.public_profiles(db, limit)


def _bad(err: CabinetError) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(err))


# ------------------------------- public --------------------------------- #
# Объявлены ДО /{orcid}: литерал "u" в первом сегменте иначе читался бы как iD.
@router.get("/u/{user_id}", response_model=ResearcherPageResponse)
async def researcher_page_by_user(user_id: str, db: AsyncSession = Depends(get_db)):
    """Карточка исследователя по id аккаунта — адрес для профиля без ORCID.

    Если ORCID у профиля всё-таки есть, отдаём и его внешние работы: страница
    по такому адресу должна показывать то же, что и /researcher/{orcid}.
    """
    profile = await domain.get_profile_by_user_id(db, user_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Researcher not found")
    orcid = profile.get("orcid")
    works = await domain.list_researcher_works(db, orcid) if orcid else []
    return {"profile": profile, "works": works}


@router.get("/u/{user_id}/publications")
async def researcher_publications_by_user(
    user_id: str, db: AsyncSession = Depends(get_db)
):
    """Публикации, привязанные к этому профилю (claim в кабинете).

    Аналог /{orcid}/publications для тех, у кого ORCID нет: там связь идёт по
    `article_authors.orcid`, здесь — по `article_authors.profile_id`.
    """
    return await _authors.list_publications_by_profile(db, user_id)


@router.get("/{orcid}", response_model=ResearcherPageResponse)
async def researcher_page(orcid: str, db: AsyncSession = Depends(get_db)):
    profile = await domain.get_researcher_profile(db, orcid)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Researcher not found")
    works = await domain.list_researcher_works(db, orcid)
    return {"profile": profile, "works": works}


@router.get("/{orcid}/publications")
async def researcher_publications(orcid: str, db: AsyncSession = Depends(get_db)):
    """Публикации на платформе, где этот ORCID указан среди авторов.

    Отличается от `works`: те импортированы из ORCID (внешние), а эти — статьи
    самой researcher.uz, связанные через `article_authors.orcid`.
    """
    return await _authors.list_publications_by_orcid(db, orcid)


# --------------------------- authenticated ------------------------------ #
@router.patch("/me/profile", status_code=204)
async def update_my_profile(
    body: UpdateMyProfileRequest,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    await domain.update_my_profile(
        db,
        user_id=profile.id,
        full_name=body.full_name,
        workplace=body.workplace,
        country=body.country,
        education=body.education,
        bio=body.bio,
        avatar_url=body.avatar_url,
    )


@router.post("/me/claim/{article_id}", status_code=204)
async def claim_article(
    article_id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    try:
        await domain.claim_article(db, user_id=profile.id, article_id=article_id)
    except CabinetError as e:
        raise _bad(e)


@router.delete("/me/claim/{article_id}", status_code=204)
async def unclaim_article(
    article_id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    await domain.unclaim_article(db, user_id=profile.id, article_id=article_id)


@router.post("/me/claim-by-dois")
async def claim_by_dois(
    body: DoisRequest,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    try:
        n = await domain.claim_articles_by_dois(db, user_id=profile.id, dois=body.dois)
    except CabinetError as e:
        raise _bad(e)
    return {"claimed": n}


@router.post("/me/import-works")
async def import_works(
    body: ImportWorksRequest,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    try:
        n = await domain.import_orcid_works(
            db, user_id=profile.id, works=[w.model_dump() for w in body.works]
        )
    except CabinetError as e:
        raise _bad(e)
    return {"count": n}

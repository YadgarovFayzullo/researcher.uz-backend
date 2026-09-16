"""Карточки авторов — публичные страницы /author/<slug>, заявки и модерация.

Читать может кто угодно: карточка адресована в том числе поисковикам, ради
которых она и заводится. Заявку подаёт вошедший, а решение по ней принимает
ТОЛЬКО владелец платформы: число публикаций попадает в аттестационные
документы, и у присвоения чужих работ есть прямая выгода.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_profile, require_owner
from src.domain.authors import AuthorCardDomain, AuthorCardError
from src.domain.notifications import (
    notify_owner_claim,
    send_claim_approved,
    send_claim_rejected,
)
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Profile

router = APIRouter()
domain = AuthorCardDomain()


def _bad(err: AuthorCardError) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(err))


# Объявлены ДО /{slug}: иначе литерал "claims" уедет в параметр слага.
@router.get("/claims")
async def list_claims(
    claim_status: str = "pending",
    db: AsyncSession = Depends(get_db),
    _: Profile = Depends(require_owner),
):
    """Очередь заявок на присвоение карточек (владелец)."""
    return await domain.list_claims(db, claim_status)


@router.post("/claims/{claim_id}/approve")
async def approve_claim(
    claim_id: str,
    background: BackgroundTasks,
    reason: str | None = Body(None, embed=True),
    db: AsyncSession = Depends(get_db),
    owner: Profile = Depends(require_owner),
):
    try:
        result = await domain.decide_claim(
            db, claim_id, approve=True, decided_by=str(owner.id), reason=reason
        )
    except AuthorCardError as err:
        raise _bad(err) from err
    # Письмо автору — фоном, после ответа: без него человек не узнает, что
    # профиль готов, а сбой почты не должен отменять само одобрение.
    background.add_task(send_claim_approved, claim_id)
    return result


@router.post("/claims/{claim_id}/reject")
async def reject_claim(
    claim_id: str,
    background: BackgroundTasks,
    reason: str | None = Body(None, embed=True),
    db: AsyncSession = Depends(get_db),
    owner: Profile = Depends(require_owner),
):
    try:
        result = await domain.decide_claim(
            db, claim_id, approve=False, decided_by=str(owner.id), reason=reason
        )
    except AuthorCardError as err:
        raise _bad(err) from err
    # С причиной и подсказкой, чем подтвердить авторство при повторной заявке.
    background.add_task(send_claim_rejected, claim_id)
    return result


@router.get("/")
async def list_authors(
    limit: int = 100, offset: int = 0, db: AsyncSession = Depends(get_db)
):
    """Индексируемые карточки — указатель, перелинковка и карта сайта."""
    return await domain.list_top(db, limit=limit, offset=offset)


@router.get("/{slug}")
async def author_card(slug: str, db: AsyncSession = Depends(get_db)):
    card = await domain.get_by_slug(db, slug)
    if card is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Author not found")
    card["publications"] = await domain.publications(db, card["id"])
    card["name_variants"] = await domain.name_variants(db, card["id"])
    return card


@router.get("/{slug}/claim")
async def my_claim(
    slug: str,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    """Состояние моей заявки — страница кэшируется, статус берём отдельно."""
    return await domain.my_claim(db, slug, str(profile.id)) or {"status": "none"}


@router.post("/{slug}/claim")
async def request_claim(
    slug: str,
    background: BackgroundTasks,
    note: str | None = Body(None, embed=True),
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    """Подать заявку. Привязки не происходит — она только после одобрения."""
    try:
        result = await domain.request_claim(db, slug, str(profile.id), note)
    except AuthorCardError as err:
        raise _bad(err) from err
    # Повторное нажатие возвращает ту же заявку — владельцу пишем только о новой.
    if result.pop("created", False):
        background.add_task(notify_owner_claim, result["claim_id"])
    return result


@router.post("/{slug}/unclaim")
async def unclaim(
    slug: str,
    db: AsyncSession = Depends(get_db),
    _: Profile = Depends(require_owner),
):
    """Отвязать карточку — на случай ошибки или спора (владелец)."""
    try:
        return await domain.unclaim(db, slug)
    except AuthorCardError as err:
        raise _bad(err) from err

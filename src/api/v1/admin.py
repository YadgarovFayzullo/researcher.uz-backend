"""Owner-консоль — эндпоинты поверх AdminDomain (порт owner_console.sql).

Все эндпоинты закрыты зависимостью `require_owner` (403 не-owner'у). Мутации
дополнительно проходят инварианты сервиса (нельзя менять owner-роль и т.п.).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_profile, require_owner
from src.domain.admin import AdminDomain, AdminError, NotOwner
from src.domain.authz import is_owner
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Journal, JournalAdmin, Profile
from src.schemas.admin import (
    EditorOption,
    JournalAdminRow,
    ProfileRow,
    SetJournalAdminRequest,
    SetUserRoleRequest,
)

router = APIRouter()
domain = AdminDomain()


@router.get("/profiles", response_model=list[ProfileRow])
async def all_profiles(
    _owner: Profile = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    return await domain.get_all_profiles(db, caller_is_owner=True)


@router.get("/journal-admins", response_model=list[JournalAdminRow])
async def all_journal_admins(
    _owner: Profile = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    return await domain.get_all_journal_admins(db, caller_is_owner=True)


@router.get("/my-journals", response_model=list[int])
async def my_journals(
    profile: Profile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
):
    """Id журналов, к которым привязан вызывающий (порт getAdminJournalIds).

    Не под `require_owner`: это как раз запрос обычного админа про себя.
    Пользователь берётся из сессии, а не из параметра — иначе любой мог бы
    спросить про чужие привязки.
    """
    if is_owner(profile.role):
        # Владелец сайта админит все журналы разом.
        return list(
            (await db.execute(select(Journal.id))).scalars().all()
        )
    return list(
        (
            await db.execute(
                select(JournalAdmin.journal_id).where(
                    JournalAdmin.user_id == profile.id
                )
            )
        )
        .scalars()
        .all()
    )


@router.get("/editor-options", response_model=list[EditorOption])
async def editor_options(
    _owner: Profile = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """get_editor_options() — справочник admin/owner для выбора редактора."""
    return await domain.get_editor_options(db, caller_is_owner=True)


@router.post("/user-role", status_code=status.HTTP_204_NO_CONTENT)
async def set_user_role(
    body: SetUserRoleRequest,
    _owner: Profile = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    try:
        await domain.set_user_role(
            db,
            caller_is_owner=True,
            target_user=body.target_user,
            new_role=body.new_role,
        )
    except NotOwner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Owner access required")
    except AdminError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.post("/journal-admin", status_code=status.HTTP_204_NO_CONTENT)
async def set_journal_admin(
    body: SetJournalAdminRequest,
    _owner: Profile = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    try:
        await domain.set_journal_admin(
            db,
            caller_is_owner=True,
            target_journal=body.target_journal,
            target_user=body.target_user,
            attach=body.attach,
        )
    except NotOwner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Owner access required")
    except AdminError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

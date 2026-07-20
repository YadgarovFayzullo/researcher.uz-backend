"""Owner-консоль — порт SECURITY DEFINER RPC из `owner_console.sql`.

`get_all_profiles`, `set_user_role`, `get_all_journal_admins`, `set_journal_admin`.
Все мутации — только owner. В SQL это гарантировал SECURITY DEFINER + проверка
role='owner' внутри функции; здесь эндпоинт закрыт зависимостью `require_owner`,
а сервис дополнительно требует `caller_is_owner=True` (защита в глубину) и хранит
бизнес-инварианты (нельзя трогать owner-строки, роль только admin/authenticated).
"""
from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.persistence.models import JournalAdmin, Profile, User


class NotOwner(Exception):
    """Вызывающий не owner."""


class AdminError(Exception):
    """Нарушение инварианта (напр. попытка изменить owner-строку)."""


def _require_owner(caller_is_owner: bool) -> None:
    if not caller_is_owner:
        raise NotOwner()


class AdminDomain:
    # ------------------------------------------------------------- reads
    async def get_all_profiles(self, db: AsyncSession, *, caller_is_owner: bool):
        """profiles + email (JOIN users), сортировка owner→admin→прочие."""
        _require_owner(caller_is_owner)
        res = await db.execute(
            select(
                Profile.id,
                Profile.full_name,
                User.email,
                Profile.role,
                Profile.created_at,
            ).join(User, User.id == Profile.id)
        )
        rows = res.all()
        order = {"owner": 0, "admin": 1}
        return sorted(
            (
                {
                    "id": r.id,
                    "full_name": r.full_name,
                    "email": r.email,
                    "role": r.role,
                    "created_at": r.created_at,
                }
                for r in rows
            ),
            key=lambda x: (
                order.get(x["role"], 2),
                (x["full_name"] or "￿").lower(),
                (x["email"] or ""),
            ),
        )

    async def get_editor_options(self, db: AsyncSession, *, caller_is_owner: bool):
        """Порт get_editor_options(): profiles с ролью admin/owner + email (owner-only).

        Справочник для выбора редактора в админ-формах. В SQL owner-гейт стоит
        внутри функции; здесь — caller_is_owner. Сортировка: full_name NULLS LAST, email.
        """
        _require_owner(caller_is_owner)
        res = await db.execute(
            select(Profile.id, Profile.full_name, User.email, Profile.role)
            .join(User, User.id == Profile.id)
            .where(Profile.role.in_(("admin", "owner")))
        )
        return sorted(
            (
                {"id": r.id, "full_name": r.full_name, "email": r.email, "role": r.role}
                for r in res.all()
            ),
            key=lambda x: ((x["full_name"] or "￿").lower(), (x["email"] or "")),
        )

    async def get_all_journal_admins(self, db: AsyncSession, *, caller_is_owner: bool):
        _require_owner(caller_is_owner)
        res = await db.execute(
            select(
                JournalAdmin.journal_id,
                JournalAdmin.user_id,
                Profile.full_name,
                User.email,
            )
            .join(Profile, Profile.id == JournalAdmin.user_id)
            .join(User, User.id == JournalAdmin.user_id)
        )
        return [
            {
                "journal_id": r.journal_id,
                "user_id": r.user_id,
                "full_name": r.full_name,
                "email": r.email,
            }
            for r in res.all()
        ]

    # ----------------------------------------------------------- mutations
    async def set_user_role(
        self, db: AsyncSession, *, caller_is_owner: bool, target_user, new_role: str
    ) -> None:
        _require_owner(caller_is_owner)
        if new_role not in ("admin", "authenticated"):
            raise AdminError("role must be admin or authenticated")

        target = (
            await db.execute(select(Profile).where(Profile.id == target_user))
        ).scalars().first()
        if target is None:
            raise AdminError("target profile not found")
        # Роль owner неприкосновенна (и защита владельца от самопонижения).
        if target.role == "owner":
            raise AdminError("cannot change an owner role")

        target.role = new_role
        await db.commit()

    async def set_journal_admin(
        self,
        db: AsyncSession,
        *,
        caller_is_owner: bool,
        target_journal: int,
        target_user,
        attach: bool,
    ) -> None:
        _require_owner(caller_is_owner)
        existing = (
            await db.execute(
                select(JournalAdmin).where(
                    JournalAdmin.journal_id == target_journal,
                    JournalAdmin.user_id == target_user,
                )
            )
        ).scalars().first()

        if attach:
            if existing is None:
                db.add(JournalAdmin(journal_id=target_journal, user_id=target_user))
            # Прикреплённый редактор должен быть admin (owner не понижаем).
            target = (
                await db.execute(select(Profile).where(Profile.id == target_user))
            ).scalars().first()
            if target is not None and target.role not in ("admin", "owner"):
                target.role = "admin"
        else:
            await db.execute(
                delete(JournalAdmin).where(
                    JournalAdmin.journal_id == target_journal,
                    JournalAdmin.user_id == target_user,
                )
            )
        await db.commit()

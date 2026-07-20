"""Авторизация — порт RLS-политик (`rls_roles.sql` / `rls_content.sql`) в код.

Чистые предикаты «можно ли этому юзеру писать в эту строку». Эндпоинты вызывают
их явно (журнал/выпуск/статья зависят от полей строки — issue_id, admin_id,
publisher_id — которые приходят в теле, не в пути, поэтому это функции, а не
FastAPI-зависимости). Простые случаи (owner-only) закрыты зависимостями в
`src/api/deps.py`.

Модель доступа (зеркалит rls_content.sql):
  journals  — только owner.
  issues    — owner ИЛИ journal_admin родительского журнала.
  articles  — owner
              ИЛИ (issue_id задан И journal_admin журнала этого выпуска)
              ИЛИ admin_id == uid
              ИЛИ (publisher_id задан И publishers.admin_id == uid).
Чтение всех трёх таблиц публично (в RLS `using (true)`), гардов на чтение нет.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.persistence.models import Issue, JournalAdmin, Publisher

# Роли, которым разрешено что-либо делать. Любое иное значение (историч. 'user')
# = заблокированный аккаунт (как middleware.ts фронта). См. get_current_profile.
VALID_ROLES = {"owner", "admin", "authenticated"}


def is_owner(role: str | None) -> bool:
    return role == "owner"


async def is_journal_admin(db: AsyncSession, user_id, journal_id) -> bool:
    """Есть ли строка journal_admins(journal_id, user_id)."""
    if journal_id is None:
        return False
    res = await db.execute(
        select(JournalAdmin.journal_id).where(
            JournalAdmin.user_id == user_id,
            JournalAdmin.journal_id == journal_id,
        )
    )
    return res.first() is not None


async def is_publisher_admin(db: AsyncSession, user_id, publisher_id) -> bool:
    """publishers.admin_id == user_id для данного издателя."""
    if publisher_id is None:
        return False
    res = await db.execute(
        select(Publisher.id).where(
            Publisher.id == publisher_id,
            Publisher.admin_id == user_id,
        )
    )
    return res.first() is not None


async def _issue_journal_id(db: AsyncSession, issue_id):
    res = await db.execute(select(Issue.journal_id).where(Issue.id == issue_id))
    row = res.first()
    return row[0] if row else None


async def can_write_journal(role: str | None) -> bool:
    """journals — только owner."""
    return is_owner(role)


async def can_write_issue(
    db: AsyncSession, *, role: str | None, user_id, journal_id
) -> bool:
    """issues — owner ИЛИ journal_admin родительского журнала."""
    if is_owner(role):
        return True
    return await is_journal_admin(db, user_id, journal_id)


async def can_write_article(
    db: AsyncSession,
    *,
    role: str | None,
    user_id,
    issue_id=None,
    admin_id=None,
    publisher_id=None,
) -> bool:
    """articles — см. модель доступа выше (короткое замыкание по OR-веткам)."""
    if is_owner(role):
        return True
    if issue_id is not None:
        journal_id = await _issue_journal_id(db, issue_id)
        if journal_id is not None and await is_journal_admin(db, user_id, journal_id):
            return True
    if admin_id is not None and admin_id == user_id:
        return True
    if publisher_id is not None and await is_publisher_admin(db, user_id, publisher_id):
        return True
    return False

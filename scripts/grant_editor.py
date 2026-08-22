"""Выдать человеку права редактора журнала.

Заводит учётку (если её ещё нет), ставит роль `admin` и привязывает к журналу
через `journal_admins` — ровно то, что делает Owner-консоль, но пригодно для
заявок вида «зарегистрируй редактора и прикрепи к журналу X».

Идемпотентно: повторный запуск не двоит привязку и не трогает пароль, если он
не задан. Роль `owner` не понижается.

Скрипт ходит прямо в БД, поэтому запускать его надо там, где задан
DATABASE_URL. На проде — внутри контейнера api:

    ssh ubuntu@<host> "cd ~/app && sudo docker compose -f docker-compose.prod.yml \\
        exec -T -e PYTHONPATH=/app -e EDITOR_PASSWORD='...' api \\
        python scripts/grant_editor.py --email a@b.uz --journal 'Название журнала'"

Пароль — только через EDITOR_PASSWORD: в аргументах он осел бы в истории
команд. Новому пользователю пароль обязателен, существующему — необязателен
(без него пароль остаётся прежним).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import uuid

from sqlalchemy import or_, select

from src.core.security import hash_password
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Identity_,
    Journal,
    JournalAdmin,
    Profile,
    User,
)


async def resolve_journal(db, ref: str) -> Journal | None:
    """Журнал по id, слагу или названию (регистр не важен)."""
    if ref.isdigit():
        found = (
            await db.execute(select(Journal).where(Journal.id == int(ref)))
        ).scalars().first()
        if found:
            return found
    rows = list(
        (
            await db.execute(
                select(Journal).where(
                    or_(Journal.slug.ilike(ref), Journal.name.ilike(ref))
                )
            )
        )
        .scalars()
        .all()
    )
    if len(rows) > 1:
        raise SystemExit(
            "Под описание подходит несколько журналов: "
            + ", ".join(f"{j.id} — {j.name}" for j in rows)
            + ". Укажите --journal <id>."
        )
    return rows[0] if rows else None


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--email", required=True)
    p.add_argument("--journal", required=True, help="id, слаг или название журнала")
    p.add_argument("--name", help="ФИО для профиля (только при создании учётки)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    email = args.email.strip().lower()
    password = os.environ.get("EDITOR_PASSWORD")

    async with AsyncSessionLocal() as db:
        journal = await resolve_journal(db, args.journal)
        if journal is None:
            raise SystemExit(f"Журнал не найден: {args.journal!r}")

        user = (
            await db.execute(select(User).where(User.email == email))
        ).scalars().first()

        if args.dry_run:
            print(f"Журнал: {journal.id} — {journal.name}")
            print(f"Учётка: {email} — {'уже есть' if user else 'будет создана'}")
            print("--dry-run: ничего не записано.")
            return 0

        if user is None:
            if not password:
                raise SystemExit(
                    "Пользователя нет — задайте пароль в переменной EDITOR_PASSWORD."
                )
            user = User(id=uuid.uuid4(), email=email, password_hash=hash_password(password))
            db.add(user)
            await db.flush()
            db.add(Profile(id=user.id, full_name=args.name, role="admin"))
            db.add(
                Identity_(
                    user_id=user.id,
                    provider="email",
                    provider_id=str(user.id),
                    identity_data={"email": email},
                )
            )
            print(f"+ учётка {email}")
        else:
            if password:
                user.password_hash = hash_password(password)
                print("= пароль обновлён")
            profile = (
                await db.execute(select(Profile).where(Profile.id == user.id))
            ).scalars().first()
            if profile is None:
                db.add(Profile(id=user.id, full_name=args.name, role="admin"))
                print("+ профиль с ролью admin")
            elif profile.role == "owner":
                # Владельцу сайта роль не понижаем: он и так админит всё.
                print("= у пользователя роль owner — оставляем как есть")
            elif profile.role != "admin":
                profile.role = "admin"
                print(f"= роль {profile.role!r} → admin")
            else:
                print("= роль admin уже стоит")

        link = (
            await db.execute(
                select(JournalAdmin).where(
                    JournalAdmin.journal_id == journal.id,
                    JournalAdmin.user_id == user.id,
                )
            )
        ).scalars().first()
        if link is None:
            db.add(JournalAdmin(journal_id=journal.id, user_id=user.id))
            print(f"+ привязка к журналу {journal.id} — {journal.name}")
        else:
            print("= привязка к журналу уже есть")

        await db.commit()

    print(f"\nГотово. Вход: {email}")
    print(f"Панель журнала: https://researcher.uz/ru/admin/journals/publisher/{journal.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

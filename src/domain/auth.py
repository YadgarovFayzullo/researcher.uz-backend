"""Доменная логика аутентификации.

Модель юзера — своя `users` + `identities` (замена Supabase auth.*). Один
пользователь может иметь несколько провайдеров (google/email/orcid); OAuth-вход
ищет по (provider, provider_id), при отсутствии — линкует к юзеру с тем же email,
иначе создаёт нового (users + profiles + identities).
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import hash_password, verify_password
from src.infrastructure.persistence.models import Identity_, Profile, User


class AuthDomain:
    # ----------------------------------------------------------- lookups
    async def get_user_by_id(self, db: AsyncSession, user_id) -> User | None:
        res = await db.execute(select(User).where(User.id == user_id))
        return res.scalars().first()

    async def get_user_by_email(self, db: AsyncSession, email: str) -> User | None:
        res = await db.execute(select(User).where(User.email == email))
        return res.scalars().first()

    async def get_identity(
        self, db: AsyncSession, provider: str, provider_id: str
    ) -> Identity_ | None:
        res = await db.execute(
            select(Identity_).where(
                Identity_.provider == provider,
                Identity_.provider_id == provider_id,
            )
        )
        return res.scalars().first()

    async def get_profile(self, db: AsyncSession, user_id) -> Profile | None:
        res = await db.execute(select(Profile).where(Profile.id == user_id))
        return res.scalars().first()

    # ----------------------------------------------------------- password
    async def authenticate(
        self, db: AsyncSession, email: str, password: str
    ) -> User | None:
        user = await self.get_user_by_email(db, email)
        if not user or not verify_password(password, user.password_hash):
            return None
        return user

    async def register(
        self, db: AsyncSession, email: str, password: str, full_name: str | None = None
    ) -> User:
        user = User(
            id=uuid.uuid4(),
            email=email,
            password_hash=hash_password(password),
        )
        db.add(user)
        await db.flush()  # получить user.id до вставки profile/identity
        db.add(Profile(id=user.id, full_name=full_name, role="authenticated"))
        db.add(
            Identity_(
                user_id=user.id,
                provider="email",
                provider_id=str(user.id),
                identity_data={"email": email},
            )
        )
        await db.commit()
        await db.refresh(user)
        return user

    # ----------------------------------------------------------- OAuth
    async def get_or_create_oauth_user(
        self,
        db: AsyncSession,
        *,
        provider: str,
        provider_id: str,
        email: str | None,
        full_name: str | None = None,
        avatar_url: str | None = None,
        identity_data: dict | None = None,
    ) -> User:
        """Найти юзера по identity → по email → создать нового. Идемпотентно."""
        # 1) уже логинился этим провайдером
        ident = await self.get_identity(db, provider, provider_id)
        if ident:
            user = await self.get_user_by_id(db, ident.user_id)
            if user:
                return user

        # 2) есть юзер с таким email — привязываем новый провайдер
        user = await self.get_user_by_email(db, email) if email else None
        if user is None:
            user = User(id=uuid.uuid4(), email=email)
            db.add(user)
            await db.flush()
            db.add(
                Profile(
                    id=user.id,
                    full_name=full_name,
                    avatar_url=avatar_url,
                    role="authenticated",
                )
            )

        db.add(
            Identity_(
                user_id=user.id,
                provider=provider,
                provider_id=str(provider_id),
                identity_data=identity_data or {},
            )
        )
        await db.commit()
        await db.refresh(user)
        return user

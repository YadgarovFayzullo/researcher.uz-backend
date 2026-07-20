"""FastAPI-зависимости аутентификации.

`get_current_user` — обязательная (401, если нет валидного access-токена),
`get_optional_user` — мягкая (None для анонима). Токен берётся из заголовка
`Authorization: Bearer <jwt>` либо из httpOnly-cookie (для запросов от Next.js).
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.security import decode_token
from src.domain.auth import AuthDomain
from src.domain.authz import VALID_ROLES, is_owner
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Profile, User

_auth = AuthDomain()


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("Authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get(settings.ACCESS_COOKIE_NAME)


async def get_optional_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User | None:
    token = _extract_token(request)
    if not token:
        return None
    payload = decode_token(token, expected_type="access")
    if not payload or not payload.get("sub"):
        return None
    return await _auth.get_user_by_id(db, payload["sub"])


async def get_current_user(
    user: User | None = Depends(get_optional_user),
) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def get_current_profile(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Profile:
    """Профиль залогиненного юзера. Аккаунт с ролью вне VALID_ROLES (историч.
    'user') считается заблокированным → 403 (как middleware.ts фронта)."""
    profile = await _auth.get_profile(db, user.id)
    role = profile.role if profile else None
    if role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is not active",
        )
    return profile


async def require_owner(
    profile: Profile = Depends(get_current_profile),
) -> Profile:
    """Только site-owner. 403 иначе."""
    if not is_owner(profile.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner access required",
        )
    return profile

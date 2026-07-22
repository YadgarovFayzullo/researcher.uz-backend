"""Эндпоинты аутентификации: register / login / refresh / logout / me.

Токены выдаются и в теле ответа (для мобильных/сторонних клиентов), и в
httpOnly-cookie (для Next.js). Пароль-вход работает для 9 перенесённых из
Supabase юзеров, как только их bcrypt-хэши добраны (Фаза 9).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user
from src.core.cookies import clear_auth_cookies, set_auth_cookies
from src.core.ratelimit import limiter
from src.core.security import create_access_token, create_refresh_token, decode_token
from src.domain.auth import AuthDomain
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import User
from src.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserMe

router = APIRouter()
domain = AuthDomain()


def _issue(response: Response, user: User) -> TokenResponse:
    access = create_access_token(str(user.id), extra={"email": user.email})
    refresh = create_refresh_token(str(user.id))
    set_auth_cookies(response, access, refresh)
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/register", response_model=TokenResponse)
@limiter.limit("5/minute")
async def register(
    request: Request,
    body: RegisterRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    if await domain.get_user_by_email(db, body.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    user = await domain.register(db, body.email, body.password, body.full_name)
    return _issue(response, user)


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute")
async def login(
    request: Request,
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    user = await domain.authenticate(db, body.email, body.password)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    return _issue(response, user)


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("30/minute")
async def refresh(
    request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
    from src.core.config import settings

    token = request.cookies.get(settings.REFRESH_COOKIE_NAME)
    if not token:
        # допускаем refresh-токен в теле-заголовке для не-браузерных клиентов
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    payload = decode_token(token, expected_type="refresh") if token else None
    if not payload or not payload.get("sub"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
    user = await domain.get_user_by_id(db, payload["sub"])
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return _issue(response, user)


@router.post("/logout")
async def logout(response: Response):
    clear_auth_cookies(response)
    return {"ok": True}


@router.get("/me", response_model=UserMe)
async def me(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    profile = await domain.get_profile(db, user.id)
    out = UserMe.model_validate(user)
    if profile:
        from src.schemas.auth import ProfileOut

        out.profile = ProfileOut(
            id=str(profile.id),
            full_name=profile.full_name,
            avatar_url=profile.avatar_url,
            role=profile.role,
            username=profile.username,
            orcid_id=profile.orcid_id,
        )
    return out

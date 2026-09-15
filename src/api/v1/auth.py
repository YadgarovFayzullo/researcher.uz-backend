"""Эндпоинты аутентификации: register / login / refresh / logout / me.

Токены выдаются и в теле ответа (для мобильных/сторонних клиентов), и в
httpOnly-cookie (для Next.js). Пароль-вход работает для 9 перенесённых из
Supabase юзеров, как только их bcrypt-хэши добраны (Фаза 9).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user
from src.core.cookies import clear_auth_cookies, set_auth_cookies
from src.core.ratelimit import limiter
from src.core.security import create_access_token, create_refresh_token, decode_token
from src.core.turnstile import client_ip, verify_token
from src.core.config import settings
from src.domain.auth import AuthDomain
from src.domain.demo import demo_user_for_login
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
    # Регистрация — разовое действие, поэтому здесь нужен свежий токен виджета,
    # а не cookie-пропуск (её бот получил бы один раз и штамповал аккаунты).
    if not await verify_token(body.turnstile_token, client_ip(request)):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Не удалось подтвердить, что вы не робот. Обновите страницу и попробуйте снова.",
        )
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


_DEMO_LOCALES = {"ru", "uz", "en", "ko", "tr"}


@router.get("/demo-login")
@limiter.limit("20/minute")
async def demo_login(
    request: Request,
    t: str = "",
    locale: str = "ru",
    db: AsyncSession = Depends(get_db),
):
    """Автовход в демо-профиль исследователя по ссылке — показ без регистрации.

    Ссылку выдаёт `scripts/create_demo_researcher.py`. Токен сверяется по SHA-256
    со сроком и работает только для профиля с пометкой demo (src/domain/demo.py):
    обычный аккаунт так не открыть. Неверная или просроченная ссылка ведёт на
    страницу входа, а не отвечает ошибкой — её откроет человек, а не клиент API.
    """
    front = settings.FRONTEND_URL.rstrip("/")
    loc = locale if locale in _DEMO_LOCALES else "ru"
    user = await demo_user_for_login(db, t)
    if user is None:
        return RedirectResponse(
            f"{front}/{loc}/auth/login?demo_error=invalid",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    resp = RedirectResponse(
        f"{front}/{loc}/researcher/u/{user.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    _issue(resp, user)
    return resp


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

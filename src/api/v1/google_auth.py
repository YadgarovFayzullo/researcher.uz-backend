"""Google OAuth2 — основной способ входа (80/86 юзеров перенесены с google-identity).

Поток: GET /auth/google/login → редирект в Google → GET /auth/google/callback
(обмен code → токен → userinfo → find/create users+identities по google `sub`
→ выдать JWT-сессию в cookie → редирект на фронт). Существующие юзеры находятся
по identity (provider='google', provider_id=<sub>), сохранённой при миграции.
"""
from __future__ import annotations

import secrets
import urllib.parse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.cookies import set_auth_cookies
from src.core.security import create_access_token, create_refresh_token
from src.domain.auth import AuthDomain
from src.infrastructure.persistence.db import get_db

router = APIRouter()
domain = AuthDomain()

_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
_STATE_COOKIE = "google_oauth_state"


def _require_config() -> None:
    if not (settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET and settings.GOOGLE_REDIRECT_URI):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Google OAuth is not configured (set GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI)",
        )


@router.get("/login")
async def google_login(request: Request):
    _require_config()
    state = secrets.token_urlsafe(24)
    # необязательный ?next= — куда вернуть на фронте после логина
    next_url = request.query_params.get("next") or settings.FRONTEND_URL
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    resp = RedirectResponse(_GOOGLE_AUTH + "?" + urllib.parse.urlencode(params))
    resp.set_cookie(
        _STATE_COOKIE, state, max_age=600, httponly=True,
        secure=settings.COOKIE_SECURE, samesite=settings.COOKIE_SAMESITE, path="/",
    )
    resp.set_cookie(
        "google_oauth_next", next_url, max_age=600, httponly=True,
        secure=settings.COOKIE_SECURE, samesite=settings.COOKIE_SAMESITE, path="/",
    )
    return resp


@router.get("/callback")
async def google_callback(
    request: Request, db: AsyncSession = Depends(get_db)
):
    _require_config()
    error = request.query_params.get("error")
    if error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Google OAuth error: {error}")

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing code/state")
    if state != request.cookies.get(_STATE_COOKIE):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid OAuth state")

    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            _GOOGLE_TOKEN,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Token exchange failed")
        access = token_resp.json().get("access_token")
        userinfo_resp = await client.get(
            _GOOGLE_USERINFO, headers={"Authorization": f"Bearer {access}"}
        )
        if userinfo_resp.status_code != 200:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Failed to fetch userinfo")
        info = userinfo_resp.json()

    sub = info.get("sub")
    if not sub:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No 'sub' in userinfo")

    user = await domain.get_or_create_oauth_user(
        db,
        provider="google",
        provider_id=sub,
        email=info.get("email"),
        full_name=info.get("name"),
        avatar_url=info.get("picture"),
        identity_data={
            "sub": sub,
            "email": info.get("email"),
            "name": info.get("name"),
            "picture": info.get("picture"),
            "email_verified": info.get("email_verified"),
        },
    )

    next_url = request.cookies.get("google_oauth_next") or settings.FRONTEND_URL
    resp = RedirectResponse(next_url, status_code=status.HTTP_302_FOUND)
    set_auth_cookies(
        resp,
        create_access_token(str(user.id), extra={"email": user.email}),
        create_refresh_token(str(user.id)),
    )
    resp.delete_cookie(_STATE_COOKIE, path="/")
    resp.delete_cookie("google_oauth_next", path="/")
    return resp

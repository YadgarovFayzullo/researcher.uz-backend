"""Cloudflare Turnstile — невидимая проверка «человек или бот».

Две разные задачи, поэтому два механизма:

* Разовое действие (регистрация) — фронт присылает токен виджета, мы меняем
  его на вердикт Cloudflare прямо в обработчике.
* Поток запросов (поиск) — токен одноразовый и живёт ~5 минут, требовать его
  на каждый запрос нельзя. Фронт один раз меняет токен на «пропуск человека»
  (`POST /security/turnstile`): короткий JWT в httpOnly-cookie, который дальше
  и предъявляется поиску.

Выключено, пока не задан TURNSTILE_SECRET_KEY: verify_token() отвечает True,
require_human() пропускает всех. Так локальная разработка и уже выкаченные
клиенты не ломаются, а включение фичи — это добавление секрета в окружение,
а не выкатка кода.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException, Request, status

from src.core.config import settings
from src.core.security import decode_token

logger = logging.getLogger(__name__)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Код в detail 403 — по нему фронт понимает, что надо решить капчу и повторить
# запрос, а не показывать пользователю «доступ запрещён».
TURNSTILE_REQUIRED = "turnstile_required"


def turnstile_enabled() -> bool:
    return bool(settings.TURNSTILE_SECRET_KEY)


async def verify_token(token: str | None, remoteip: str | None = None) -> bool:
    """Проверяет токен виджета в Cloudflare. False — не пускать.

    Сетевой сбой считаем провалом проверки: пропускать всех при недоступном
    siteverify — ровно то, чем воспользуется бот.
    """
    if not turnstile_enabled():
        return True
    if not token:
        return False

    data = {"secret": settings.TURNSTILE_SECRET_KEY, "response": token}
    if remoteip:
        data["remoteip"] = remoteip

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(SITEVERIFY_URL, data=data)
            res.raise_for_status()
            payload = res.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("turnstile siteverify failed: %s", exc)
        return False

    if not payload.get("success"):
        logger.info("turnstile rejected: %s", payload.get("error-codes"))
        return False
    return True


def client_ip(request: Request) -> str | None:
    """IP клиента с учётом прокси (uvicorn --proxy-headers уже разбирает XFF)."""
    return request.client.host if request.client else None


# ------------------------------------------------------- «пропуск человека»

def has_human_pass(request: Request) -> bool:
    token = request.cookies.get(settings.HUMAN_COOKIE_NAME)
    return bool(token) and decode_token(token, expected_type="human") is not None


def is_authenticated(request: Request) -> bool:
    """Залогиненного не проверяем: аккаунт уже прошёл капчу при регистрации."""
    token = request.cookies.get(settings.ACCESS_COOKIE_NAME)
    return bool(token) and decode_token(token, expected_type="access") is not None


async def require_human(request: Request) -> None:
    """Зависимость FastAPI для «шумных» публичных эндпоинтов (поиск).

    Пускает по cookie-пропуску или по сессии; иначе 403 с кодом, на который
    фронт отвечает решением капчи и повтором запроса.
    """
    if not turnstile_enabled():
        return
    if has_human_pass(request) or is_authenticated(request):
        return
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        {"code": TURNSTILE_REQUIRED, "message": "Turnstile verification required"},
    )

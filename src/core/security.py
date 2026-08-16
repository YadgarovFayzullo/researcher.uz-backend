"""Криптопримитивы аутентификации: bcrypt-пароли + JWT (access/refresh).

Пароли: формат bcrypt `$2a$/$2b$/$2y$` — совместим с хэшами из Supabase
`auth.users.encrypted_password`, поэтому 9 перенесённых юзеров логинятся без ресета.
JWT: HS256, подпись через settings.SECRET_KEY; access — короткий, refresh — длинный.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from src.core.config import settings

# bcrypt ограничен 72 байтами пароля — длинные усечём (как делает большинство реализаций).
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    pw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        pw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
        return bcrypt.checkpw(pw, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------- JWT

def _encode(payload: dict, expires: timedelta, token_type: str) -> str:
    now = datetime.now(timezone.utc)
    to_encode = {
        **payload,
        "iat": now,
        "exp": now + expires,
        "type": token_type,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(user_id: str, extra: dict | None = None) -> str:
    return _encode(
        {"sub": str(user_id), **(extra or {})},
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "access",
    )


def create_refresh_token(user_id: str) -> str:
    return _encode(
        {"sub": str(user_id)},
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        "refresh",
    )


def create_human_token() -> str:
    """«Пропуск человека» после Turnstile — без sub, он ничей и ничего не даёт,
    кроме права дёргать публичные эндпоинты, закрытые от ботов."""
    return _encode(
        {}, timedelta(hours=settings.HUMAN_PASS_EXPIRE_HOURS), "human"
    )


def decode_token(token: str, expected_type: str | None = None) -> dict | None:
    """Проверяет подпись/срок; при expected_type сверяет тип. None — если невалиден."""
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.PyJWTError:
        return None
    if expected_type and payload.get("type") != expected_type:
        return None
    return payload

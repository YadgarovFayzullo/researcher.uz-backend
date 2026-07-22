"""Rate-limiting (slowapi). Ключ — IP клиента.

ВАЖНО: работает корректно только с `uvicorn --proxy-headers` (см.
docker-entrypoint.sh) — иначе get_remote_address вернёт IP docker-гейтвея и
лимит будет общим на всех. Лимитер подключается в src/main.py; отдельные
эндпоинты (логин/регистрация) декорируются @limiter.limit.
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

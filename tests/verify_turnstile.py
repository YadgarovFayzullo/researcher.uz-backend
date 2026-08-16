"""Проверка антибот-слоя Turnstile (без сети и без БД).

Гоняет ровно то, что решает, пустить запрос или нет: выключенный режим,
cookie-«пропуск человека», обход по сессии и отказ анониму.

    PYTHONPATH=. .venv/bin/python tests/verify_turnstile.py
"""
import asyncio

from fastapi import HTTPException

from src.core.config import settings
from src.core.security import create_access_token, create_human_token
from src.core.turnstile import (
    TURNSTILE_REQUIRED,
    has_human_pass,
    is_authenticated,
    require_human,
    turnstile_enabled,
    verify_token,
)

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results = []


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  [{PASS if ok else FAIL}] {name}: got={got} want={want}")


class FakeRequest:
    """Минимум, который читает turnstile.py: cookies и client.host."""

    def __init__(self, cookies=None):
        self.cookies = cookies or {}
        self.client = None


async def allowed(request) -> bool:
    try:
        await require_human(request)
        return True
    except HTTPException as exc:
        detail = exc.detail
        return not (isinstance(detail, dict) and detail.get("code") == TURNSTILE_REQUIRED)


async def main():
    original = settings.TURNSTILE_SECRET_KEY

    print("\n— выключено (секрет не задан) —")
    settings.TURNSTILE_SECRET_KEY = None
    check("turnstile_enabled", turnstile_enabled(), False)
    check("verify_token(None) пропускает", await verify_token(None), True)
    check("аноним проходит", await allowed(FakeRequest()), True)

    print("\n— включено —")
    settings.TURNSTILE_SECRET_KEY = "test-secret"
    check("turnstile_enabled", turnstile_enabled(), True)
    check("verify_token(None) режет", await verify_token(None), False)
    check("аноним не проходит", await allowed(FakeRequest()), False)

    human = create_human_token()
    check(
        "пропуск валиден",
        has_human_pass(FakeRequest({settings.HUMAN_COOKIE_NAME: human})),
        True,
    )
    check(
        "с пропуском проходит",
        await allowed(FakeRequest({settings.HUMAN_COOKIE_NAME: human})),
        True,
    )
    check(
        "мусор вместо пропуска",
        has_human_pass(FakeRequest({settings.HUMAN_COOKIE_NAME: "garbage"})),
        False,
    )
    # Access-токен не должен подходить как пропуск и наоборот: типы разные.
    access = create_access_token("00000000-0000-0000-0000-000000000000")
    check(
        "access не сходит за пропуск",
        has_human_pass(FakeRequest({settings.HUMAN_COOKIE_NAME: access})),
        False,
    )
    check(
        "пропуск не сходит за сессию",
        is_authenticated(FakeRequest({settings.ACCESS_COOKIE_NAME: human})),
        False,
    )
    check(
        "залогиненный проходит без капчи",
        await allowed(FakeRequest({settings.ACCESS_COOKIE_NAME: access})),
        True,
    )

    settings.TURNSTILE_SECRET_KEY = original
    print(f"\n{sum(results)}/{len(results)} passed")
    raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())

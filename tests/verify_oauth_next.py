"""Возврат на карточку после входа и защита от открытого редиректа.

Кнопка «Это я» шлёт анонима на /auth/login?next=/uz/author/<slug>, и этот адрес
должен пережить вход, в том числе через Google и ORCID. При этом `next` —
параметр из ссылки, то есть чужой ввод: без проверки ручка входа превращается в
открытый редирект (ссылка на наш домен, а после входа человек на чужом сайте).

Что стережём:
  * внутренний путь превращается в адрес фронта;
  * чужой хост, протокол-относительный «//evil.com» и «/\\evil.com» отбрасываются;
  * пустой и отсутствующий next дают главную.

Живая БД не нужна. PYTHONPATH=. python tests/verify_oauth_next.py
"""
from __future__ import annotations

from src.core.config import settings
from src.core.redirects import safe_next

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results: list[bool] = []


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  [{PASS if ok else FAIL}] {name}: got={got!r} want={want!r}")


def main() -> int:
    front = settings.FRONTEND_URL.rstrip("/")

    print("\n--- свои адреса ---")
    check("карточка автора", safe_next("/uz/author/ivanov-i-i"), f"{front}/uz/author/ivanov-i-i")
    check("профиль", safe_next("/ru/researcher/0000-0002-1825-0097"), f"{front}/ru/researcher/0000-0002-1825-0097")
    check("путь с параметрами", safe_next("/uz/search?q=x"), f"{front}/uz/search?q=x")

    print("\n--- чужое и пустое ---")
    for raw in ("https://evil.com", "//evil.com", "/\\evil.com", "http://evil.com/x",
                "javascript:alert(1)", "evil.com", "", None, "   "):
        check(f"{raw!r} → главная", safe_next(raw), front)

    total_n, ok_n = len(results), sum(results)
    print(f"\n{'='*46}\nИтог: {ok_n}/{total_n} " + ("— всё зелёное" if ok_n == total_n else "— ЕСТЬ ПАДЕНИЯ"))
    return 0 if ok_n == total_n else 1


if __name__ == "__main__":
    raise SystemExit(main())

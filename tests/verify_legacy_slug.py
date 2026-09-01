"""Резолв старого адреса статьи → актуальный слаг (301 со страницы статьи).

Что стережём:
  * старый адрес с числовым хвостом находит статью БЕЗ хвоста
    (`...-imkoniyatlari-106415` → `...-imkoniyatlari`) — самый частый случай
    после переезда, раньше он давал 404: LIKE «prefix-%» не матчит саму основу;
  * обратный случай (хвост появился) и смена хвоста;
  * неоднозначность (два кандидата с одной основой) резолв не делает;
  * более длинный слаг с той же приставкой кандидатом не считается.

БД не нужна: сессия подменена — WHERE эмулируется на списке слагов, а сам
SQL проверяется на наличие ветки равенства (той, которой не хватало).
"""
from __future__ import annotations

import asyncio

from sqlalchemy.dialects import postgresql

from src.domain.article import ArticleDomain, _slug_stem

GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"
results: list[bool] = []


def check(name: str, got, want):
    ok = got == want
    results.append(ok)
    tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"{tag} {name}" + ("" if ok else f"  got={got!r} want={want!r}"))


class FakeResult:
    def __init__(self, rows): self._rows = rows
    def scalars(self): return self
    def all(self): return self._rows


class FakeSession:
    """Отдаёт слаги, попадающие под WHERE, и сохраняет текст запроса."""

    def __init__(self, slugs: list[str]):
        self.slugs = slugs
        self.sql = ""

    async def execute(self, stmt):
        self.sql = str(stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        ))
        stem = self.sql.split("articles.slug = '")[1].split("'")[0]
        return FakeResult([
            s for s in self.slugs if s == stem or s.startswith(f"{stem}-")
        ])


def resolve(slugs: list[str], slug: str) -> tuple[str | None, str]:
    db = FakeSession(slugs)
    got = asyncio.run(ArticleDomain().resolve_legacy_slug(db, slug))
    return got, db.sql


BASE = "kambag-allikni-qisqartirishda-qishloq-turizmidan-foydalanish-imkoniyatlari"

check("_slug_stem срезает числовой хвост", _slug_stem(f"{BASE}-106415"), BASE)
check("_slug_stem не трогает слаг без хвоста", _slug_stem(BASE), BASE)
check("_slug_stem не режет слово с цифрой", _slug_stem("covid-19-tahlili"), "covid-19-tahlili")

got, sql = resolve([BASE], f"{BASE}-106415")
check("хвост потерялся → находим основу", got, BASE)
check("в SQL есть ветка равенства основе", f"articles.slug = '{BASE}'" in sql, True)

check("хвост появился", resolve([f"{BASE}-4242"], BASE)[0], f"{BASE}-4242")
check("хвост сменился", resolve([f"{BASE}-4242"], f"{BASE}-106415")[0], f"{BASE}-4242")
check("совпадение с самим собой не редиректит", resolve([BASE], BASE)[0], None)
check("два кандидата — резолва нет", resolve([BASE, f"{BASE}-2"], f"{BASE}-106415")[0], None)
check("нет кандидатов", resolve([], f"{BASE}-106415")[0], None)
check(
    "длинный слаг с той же приставкой не кандидат",
    resolve([f"{BASE}-tahlili"], f"{BASE}-106415")[0],
    None,
)
check("пустой слаг", resolve([BASE], "")[0], None)

print(f"\n{sum(results)}/{len(results)} прошло")
raise SystemExit(0 if all(results) else 1)

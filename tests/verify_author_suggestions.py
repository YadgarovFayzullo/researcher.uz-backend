"""Подсказка «возможно, это ваши работы» в профиле исследователя.

Что стережём:

  * ФИО профиля находит карточку, записанную фамилией с инициалами, в любом
    порядке слов и алфавите («Fayzullo Yadgarov» ↔ «Yadgarov F.», «Ядгаров Ф.Б.»);
  * однофамилец с другим инициалом и имя из одного слова не подсказываются;
  * присвоенная кем-то карточка и карточка с отклонённой заявкой не подсказываются,
    а с заявкой на рассмотрении — подсказываются со статусом `pending`;
  * ручка закрыта для анонима.

Нужна живая БД. PYTHONPATH=. .venv/bin/python tests/verify_author_suggestions.py
"""
from __future__ import annotations

import asyncio
import uuid

import httpx
from httpx import ASGITransport
from sqlalchemy import delete, select

from src.domain.researcher import ResearcherDomain, card_matches_name
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import Author, AuthorClaim, Profile, User
from src.main import app

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results: list[bool] = []

# Фамилия, которой заведомо нет в базе: фикстуры находятся и убираются по ней.
SURNAME = "zzqxwarov"


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  [{PASS if ok else FAIL}] {name}: got={got!r} want={want!r}")


async def _sweep(db, user_ids):
    author_ids = (
        await db.execute(select(Author.id).where(Author.name_key.like(f"%{SURNAME}%")))
    ).scalars().all()
    for stmt in (
        delete(AuthorClaim).where(AuthorClaim.author_id.in_(author_ids)),
        delete(Author).where(Author.id.in_(author_ids)),
        delete(Profile).where(Profile.id.in_(user_ids)),
        delete(User).where(User.id.in_(user_ids)),
    ):
        await db.execute(stmt)
    await db.commit()


def pure_checks():
    print("\n--- сопоставление имени ---")
    cases = [
        ("yadgarov|f", "Fayzullo Yadgarov", True),
        ("yadgarov|f", "Yadgarov Fayzullo", True),
        ("yadgarov|b.f", "Fayzullo Yadgarov", True),  # в подписи есть отчество
        ("yadgarov|f", "Yadgarov Fayzullo Bahodirovich", True),  # в профиле есть
        ("yadgarov|b.f", "Ядгаров Файзулло Баходирович", True),  # кириллица
        ("fayzullo+yadgarov", "Yadgarov Fayzullo", True),
        ("fayzullo+yadgarov", "Yadgarov Fayzullo Bahodirovich", True),
        ("rasulov|m.s", "Rasulov Shavkat Murodovich", True),  # «Sh.» → s
        ("yadgarov|a", "Fayzullo Yadgarov", False),  # другой инициал
        ("yadgarov|a.b", "Yadgarov Fayzullo Bahodirovich", False),  # спор инициалов
        ("yadgarov|f", "Yadgarov", False),  # одно слово
        ("yadgarov|f", "", False),
        ("fayzullo+karimov", "Yadgarov Fayzullo", False),
        ("yadgarov", "Fayzullo Yadgarov", False),  # карточка без инициалов
    ]
    for key, name, want in cases:
        check(f"{key!r} ~ {name!r}", card_matches_name(key, name), want)

    print("\n--- полное написание карточки ---")
    # Случаи с живой базы: ключ совпадает, а имя в подписи — другое.
    with_display = [
        ("boymatov|b.e", "Bekzod Boymatov", "Boymatov Bahrom Eshmamatovich", False),
        ("umarov|a.k", "Kamoliddin Umarov", "Umarov Khusan Abdurakhimovich", False),
        ("olimova|m.m", "Маъмура Олимова", "Olimova Madinabonu Maxmudovna", False),
        ("boymatov|b.b", "Bekzod Boymatov", "Boymatov Bekzod Bahodirovich", True),
        ("ochilov|e.f", "Фарход Очилов", "Ochilov Farhod Egamberdiyevich", True),
        ("olimova|h.m", "Маъмура Олимова", "Olimova Ma'mura Homidjon qizi", True),
        ("yadgarov|b.f", "Fayzullo Yadgarov", "Yadgarov F.B.", True),  # одни инициалы
    ]
    for key, name, display, want in with_display:
        check(f"{display!r} ~ {name!r}", card_matches_name(key, name, display), want)


async def main() -> int:
    pure_checks()

    me = uuid.uuid4()
    other = uuid.uuid4()
    one_word = uuid.uuid4()
    user_ids = [me, other, one_word]
    domain = ResearcherDomain()

    async with AsyncSessionLocal() as db:
        await _sweep(db, user_ids)

        db.add_all([User(id=uid, email=f"sugg-{uid.hex[:8]}@example.com") for uid in user_ids])
        await db.flush()
        db.add_all([
            Profile(id=me, full_name=f"Bekzod {SURNAME.capitalize()}", role="authenticated"),
            Profile(id=other, full_name="Someone Else", role="authenticated"),
            Profile(id=one_word, full_name=SURNAME.capitalize(), role="authenticated"),
        ])
        await db.flush()

        def card(key: str, works: int, owner=None) -> Author:
            return Author(
                name_key=key,
                slug=key.replace("|", "-").replace("+", "-").replace(".", ""),
                display_name=key,
                works_count=works,
                profile_id=owner,
            )

        plain = card(f"{SURNAME}|b", 3)
        pending = card(f"{SURNAME}|b.t", 5)
        rejected = card(f"{SURNAME}|b.k", 7)
        stranger = card(f"{SURNAME}|a", 9)
        taken = card(f"bekzod+{SURNAME}", 11, owner=other)
        db.add_all([plain, pending, rejected, stranger, taken])
        await db.flush()
        db.add_all([
            AuthorClaim(author_id=pending.id, profile_id=me, status="pending"),
            AuthorClaim(author_id=rejected.id, profile_id=me, status="rejected"),
        ])
        await db.commit()

        print("\n--- подбор карточек ---")
        got = await domain.suggest_author_cards(db, user_id=me)
        check("нашлись ровно свои", [s["slug"] for s in got], [pending.slug, plain.slug])
        check("статус заявки на рассмотрении", got[0]["claim_status"], "pending")
        check("статус без заявки", got[1]["claim_status"], "none")
        check("число работ отдано", got[1]["works_count"], 3)

        check(
            "имя из одного слова — пусто",
            await domain.suggest_author_cards(db, user_id=one_word),
            [],
        )

        print("\n--- ручка ---")
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver", timeout=60
        ) as http:
            r = await http.get("/researcher/me/author-suggestions")
            check("аноним не пропущен", r.status_code in (401, 403), True)

        print("\n--- уборка ---")
        await _sweep(db, user_ids)
        left = (
            await db.execute(select(Author).where(Author.name_key.like(f"%{SURNAME}%")))
        ).scalars().all()
        check("фикстур не осталось", len(left), 0)

    total_n, ok_n = len(results), sum(results)
    print(f"\n{'='*46}\nИтог: {ok_n}/{total_n} " + ("— всё зелёное" if ok_n == total_n else "— ЕСТЬ ПАДЕНИЯ"))
    return 0 if ok_n == total_n else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

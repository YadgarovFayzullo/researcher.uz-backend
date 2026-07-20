"""Сверка счётчиков Supabase ↔ локальная БД (read-only).

Запускать перед боевым переключением (Фаза 9), чтобы одним прогоном увидеть,
всё ли доехало из источника. Ничего НЕ меняет — только считает строки.

Supabase-счётчики берём через PostgREST с service-role ключом (RLS не занижает),
`auth.users` — через GoTrue admin API (это не public-схема). Локальные — прямым
count(*). Для каждой таблицы печатаем source/target/дельту.

Дельта > 0 = в источнике появились строки после снимка (норма до финальной
догрузки). `article_interactions` — append-only лог, у него дельта ожидаема
всегда. `alembic_version` — локальная служебная, в источнике её нет.

Запуск:
  PYTHONPATH=. .venv/bin/python scripts/verify_delta.py
Требует SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (берём из .env.local фронта,
если не в окружении).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
from sqlalchemy import text

from src.infrastructure.persistence.db import engine

# Контентные таблицы, которые в Supabase лежат в public-схеме (сравнимы через REST).
# users/identities — в Supabase это auth-схема, считаем отдельно; alembic_version
# локальная служебная и в сверку не входит.
PUBLIC_TABLES = [
    "journals",
    "issues",
    "articles",
    "article_authors",
    "article_references",
    "article_interactions",  # append-only: дельта ожидаема
    "conference_sections",
    "publishers",
    "external_citations",
    "profiles",
    "profile_stats",
    "journal_admins",
    "researcher_works",
    "saved_articles",
    "affiliations",
    "research_interests",
    "achievements",
    "social_links",
]

APPEND_ONLY = {"article_interactions"}


def _load_supabase_creds() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if url and key:
        return url.rstrip("/"), key
    # фолбэк: .env.local фронта
    front_env = Path(__file__).resolve().parents[2] / "researcher-uz" / ".env.local"
    front_pub = Path(__file__).resolve().parents[2] / "researcher-uz" / ".env"
    vals: dict[str, str] = {}
    for f in (front_pub, front_env):
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"')
    url = url or vals.get("NEXT_PUBLIC_SUPABASE_URL", "")
    key = key or vals.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        sys.exit(
            "Нужны SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (в окружении или в "
            "researcher-uz/.env.local). service-role ключ — чтобы RLS не занижал счёт."
        )
    return url.rstrip("/"), key


def _supabase_count(client: httpx.Client, url: str, key: str, table: str) -> int | None:
    """Точный count строки через PostgREST (Content-Range: 0-0/N)."""
    r = client.get(
        f"{url}/rest/v1/{table}",
        params={"select": "*"},
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Prefer": "count=exact",
            "Range": "0-0",
        },
    )
    if r.status_code >= 400:
        return None
    cr = r.headers.get("content-range", "")
    if "/" not in cr:
        return None
    tail = cr.split("/")[-1]
    return int(tail) if tail.isdigit() else None


def _auth_users_count(client: httpx.Client, url: str, key: str) -> int | None:
    """auth.users через admin API — пагинация, пока страница не короче per_page."""
    total, page, per = 0, 1, 1000
    while True:
        r = client.get(
            f"{url}/auth/v1/admin/users",
            params={"page": page, "per_page": per},
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
        )
        if r.status_code >= 400:
            return None
        users = r.json().get("users", [])
        total += len(users)
        if len(users) < per:
            return total
        page += 1


async def main() -> int:
    url, key = _load_supabase_creds()

    async with engine.connect() as conn:
        local: dict[str, int] = {}
        for t in PUBLIC_TABLES + ["users"]:
            local[t] = (
                await conn.execute(text(f"select count(*) from {t}"))
            ).scalar_one()

    rows: list[tuple[str, int | None, int, str]] = []
    with httpx.Client(timeout=30) as client:
        for t in PUBLIC_TABLES:
            src = _supabase_count(client, url, key, t)
            rows.append((t, src, local[t], "append-only" if t in APPEND_ONLY else ""))
        # auth.users ↔ локальные users
        rows.append(("users (auth.users)", _auth_users_count(client, url, key), local["users"], ""))

    print(f"{'таблица':26} {'supabase':>9} {'локально':>9} {'дельта':>8}  примечание")
    print("-" * 70)
    drift = 0
    for name, src, loc, note in rows:
        if src is None:
            print(f"{name:26} {'—':>9} {loc:>9} {'?':>8}  нет доступа/таблицы в источнике")
            continue
        delta = src - loc
        if delta != 0 and note != "append-only":
            drift += 1
        mark = "" if delta == 0 else ("↑" if delta > 0 else "↓ ЛОКАЛЬНО БОЛЬШЕ!")
        print(f"{name:26} {src:>9} {loc:>9} {delta:>+8}  {note} {mark}".rstrip())
    print("-" * 70)
    print(
        "Всё сошлось." if drift == 0
        else f"Таблиц с невыгруженной дельтой: {drift} — прогнать ETL перед переключением."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

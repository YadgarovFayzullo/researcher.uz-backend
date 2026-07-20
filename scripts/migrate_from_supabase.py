"""
Фаза 2 — миграция данных из Supabase в свой Postgres.

Источник:
  - auth.users / identities  -> Supabase Admin Auth API (service_role key)
  - public.*                 -> Supabase REST (PostgREST, service_role key)

Приёмник:
  - локальный Postgres (scientific_db), схема уже накачена alembic (Фаза 1)

Особенности:
  - tsvector-колонки (document_*, search_vector) НЕ переносятся — пересборка в Фазе 7.
  - IDENTITY-колонки вставляются через OVERRIDING SYSTEM VALUE, затем reset sequence.
  - На время загрузки session_replication_role='replica' (postgres = superuser),
    чтобы отключить проверку FK/триггеров и не зависеть от порядка. FK-целостность
    проверяется отдельно после загрузки.
  - password_hash у users не заполняется (9 bcrypt-хэшей добираются в Фазе 3).

Запуск:
  .venv/bin/python scripts/migrate_from_supabase.py
Требуются переменные окружения (или .env.local фронтенда):
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, LOCAL_DATABASE_URL (опц.)
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.request
import urllib.error
from pathlib import Path

import psycopg2
import psycopg2.extras

# python.org-сборка на macOS часто без корневых сертификатов -> берём certifi,
# иначе (одноразовая миграция из собственного Supabase) отключаем верификацию.
try:
    import certifi  # type: ignore
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    _SSL_CTX = ssl._create_unverified_context()

# ------------------------------------------------------------------ config

FRONTEND_ENV = Path("/Users/fayulloyadgarov/researcher-uz/.env.local")
FRONTEND_ENV_PUBLIC = Path("/Users/fayulloyadgarov/researcher-uz/.env")

LOCAL_DSN = os.environ.get(
    "LOCAL_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/scientific_db",
)

# Порядок загрузки public.* (FK-безопасный, хотя FK на время выключены).
PUBLIC_TABLES = [
    "profiles",
    "journals",
    "publishers",
    "issues",
    "conference_sections",
    "articles",
    "article_authors",
    "article_interactions",
    "article_references",
    "external_citations",
    "saved_articles",
    "profile_stats",
    "achievements",
    "affiliations",
    "social_links",
    "research_interests",
    "journal_admins",
    "researcher_works",
]

PAGE = 1000  # PostgREST max rows per request


# ------------------------------------------------------------------ helpers

def load_env() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    # fallback: читаем из env-файлов фронтенда
    def read(path: Path, name: str) -> str | None:
        if not path.exists():
            return None
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line[len(name) + 1:].strip()
        return None

    if not url:
        url = read(FRONTEND_ENV_PUBLIC, "NEXT_PUBLIC_SUPABASE_URL") or read(
            FRONTEND_ENV, "NEXT_PUBLIC_SUPABASE_URL"
        )
    if not key:
        key = read(FRONTEND_ENV, "SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        sys.exit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY не найдены")
    return url.rstrip("/"), key


def http_get(url: str, key: str, extra_headers: dict | None = None):
    req = urllib.request.Request(url)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    if extra_headers:
        for k, v in extra_headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
        body = resp.read().decode()
        headers = dict(resp.getheaders())
    return body, headers


def fetch_rest_all(base: str, key: str, table: str, order_col: str) -> list[dict]:
    """Все строки таблицы через PostgREST с пагинацией (стабильный порядок по order_col)."""
    rows: list[dict] = []
    offset = 0
    while True:
        url = f"{base}/rest/v1/{table}?select=*&order={order_col}"
        hdr = {"Range-Unit": "items", "Range": f"{offset}-{offset + PAGE - 1}"}
        body, headers = http_get(url, key, hdr)
        chunk = json.loads(body)
        rows.extend(chunk)
        if len(chunk) < PAGE:
            break
        offset += PAGE
    return rows


def fetch_auth_users(base: str, key: str) -> list[dict]:
    users: list[dict] = []
    page = 1
    while True:
        url = f"{base}/auth/v1/admin/users?page={page}&per_page=1000"
        body, _ = http_get(url, key)
        data = json.loads(body)
        chunk = data.get("users", [])
        if not chunk:
            break
        users.extend(chunk)
        if len(chunk) < 1000:
            break
        page += 1
    # list-эндпоинт НЕ отдаёт identities -> добираем по каждому юзеру
    for u in users:
        if not u.get("identities"):
            body, _ = http_get(f"{base}/auth/v1/admin/users/{u['id']}", key)
            u["identities"] = json.loads(body).get("identities") or []
    return users


# ------------------------------------------------------------------ target introspection

def target_columns(cur, table: str) -> dict:
    cur.execute(
        """
        SELECT column_name, data_type, is_identity, is_generated
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        """,
        (table,),
    )
    return {r[0]: {"type": r[1], "identity": r[2] == "YES", "generated": r[3] == "ALWAYS"}
            for r in cur.fetchall()}


def insertable_columns(colmeta: dict) -> tuple[list[str], bool]:
    """Колонки для вставки: без tsvector и без GENERATED ALWAYS. Флаг identity."""
    cols = []
    has_identity = False
    for name, m in colmeta.items():
        if m["type"] == "tsvector" or m["generated"]:
            continue
        cols.append(name)
        if m["identity"]:
            has_identity = True
    return cols, has_identity


def insert_rows(cur, table: str, rows: list[dict], colmeta: dict) -> int:
    if not rows:
        return 0
    cols, has_identity = insertable_columns(colmeta)
    # берём только те колонки, что реально есть в источнике
    src_keys = set(rows[0].keys())
    use_cols = [c for c in cols if c in src_keys]
    override = "OVERRIDING SYSTEM VALUE " if has_identity else ""
    collist = ", ".join(f'"{c}"' for c in use_cols)
    placeholders = ", ".join(["%s"] * len(use_cols))
    sql = (
        f'INSERT INTO public.{table} ({collist}) {override}'
        f'VALUES ({placeholders}) ON CONFLICT DO NOTHING'
    )
    values = []
    for r in rows:
        row_vals = []
        for c in use_cols:
            v = r.get(c)
            # jsonb / массивы -> psycopg2 сам не сериализует dict/list как json,
            # но list -> Postgres array; dict -> нужно json. Определим по типу колонки.
            t = colmeta[c]["type"]
            if isinstance(v, (dict, list)) and t in ("jsonb", "json"):
                v = json.dumps(v)
            row_vals.append(v)
        values.append(tuple(row_vals))
    psycopg2.extras.execute_batch(cur, sql, values, page_size=500)
    return len(values)


def build_users_identities(auth_users: list[dict]):
    users = []
    identities = []
    for u in auth_users:
        users.append(
            (
                u["id"],
                u.get("email"),
                u.get("email_confirmed_at"),
                u.get("created_at"),
            )
        )
        for ident in (u.get("identities") or []):
            provider = ident.get("provider")
            # provider_id: у email это = user_id, у google — sub из identity_data
            pid = ident.get("provider_id") or ident.get("id")
            identities.append((u["id"], provider, str(pid)))
    return users, identities


# ------------------------------------------------------------------ sequences

def reset_sequences(cur):
    cur.execute(
        """
        SELECT c.table_name, c.column_name
        FROM information_schema.columns c
        WHERE c.table_schema='public' AND c.is_identity='YES'
        """
    )
    idcols = cur.fetchall()
    for table, col in idcols:
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence('public.{table}', %s), "
            f"COALESCE((SELECT MAX(\"{col}\") FROM public.{table}), 1), true)",
            (col,),
        )


# ------------------------------------------------------------------ main

def main():
    base, key = load_env()
    print(f"Supabase: {base}")
    conn = psycopg2.connect(LOCAL_DSN)
    conn.autocommit = False
    cur = conn.cursor()

    # отключаем FK/триггеры на время bulk-load
    cur.execute("SET session_replication_role = 'replica'")

    # 1) users + identities из Admin Auth API
    print("\n== auth users ==")
    auth_users = fetch_auth_users(base, key)
    print(f"  fetched {len(auth_users)} users")
    users, identities = build_users_identities(auth_users)
    psycopg2.extras.execute_batch(
        cur,
        'INSERT INTO public.users (id, email, email_confirmed_at, created_at) '
        'VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING',
        users,
        page_size=500,
    )
    psycopg2.extras.execute_batch(
        cur,
        'INSERT INTO public.identities (id, user_id, provider, provider_id) '
        'VALUES (gen_random_uuid(),%s,%s,%s) ON CONFLICT DO NOTHING',
        identities,
        page_size=500,
    )
    print(f"  inserted users={len(users)} identities={len(identities)}")

    # 2) public.* через REST
    summary = {}
    for table in PUBLIC_TABLES:
        colmeta = target_columns(cur, table)
        order_col = "id" if "id" in colmeta else next(iter(colmeta))
        rows = fetch_rest_all(base, key, table, order_col)
        n = insert_rows(cur, table, rows, colmeta)
        summary[table] = (len(rows), n)
        print(f"  {table:24s} fetched={len(rows):6d} inserted={n:6d}")

    # 3) sequences
    reset_sequences(cur)
    cur.execute("SET session_replication_role = 'origin'")
    conn.commit()

    # 4) сверка счётчиков
    print("\n== target counts ==")
    cur.execute("SELECT count(*) FROM public.users")
    print(f"  users {cur.fetchone()[0]}")
    cur.execute("SELECT count(*) FROM public.identities")
    print(f"  identities {cur.fetchone()[0]}")
    for table in PUBLIC_TABLES:
        cur.execute(f"SELECT count(*) FROM public.{table}")
        print(f"  {table:24s} {cur.fetchone()[0]}")

    cur.close()
    conn.close()
    print("\nDONE")


if __name__ == "__main__":
    main()

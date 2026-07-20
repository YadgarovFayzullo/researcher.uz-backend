"""
Фаза 6 — перенос файлов из Supabase Storage в Cloudflare R2 + переписывание
URL картинок в своей БД.

Стратегия (решение владельца): PDF остаются за бэкенд-прокси /pdf/<slug> (ключ
извлекается из сохранённого URL — эти поля НЕ трогаем), а обложки/логотипы/аватары
переписываются на публичные R2-URL для прямой раздачи.

Бакеты Supabase (`pdfs`, `cover`, `avatars`) → единый R2-бакет как префиксы
ключей: `pdfs/<file>`, `cover/<file>`, `avatars/<file>` (имена уникальны).

Режимы:
  copy         — (по умолчанию) скопировать все объекты Supabase Storage → R2.
  verify       — сверить число объектов и суммарный размер (Supabase vs R2).
  rewrite-urls — заменить image-URL в scientific_db на R2 public-URL.

Запуск:
  .venv/bin/python scripts/migrate_storage_to_r2.py copy
  .venv/bin/python scripts/migrate_storage_to_r2.py verify
  .venv/bin/python scripts/migrate_storage_to_r2.py rewrite-urls [--dry-run]

Требуется:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (или .env.local фронта),
  R2_* (из .env бэкенда), LOCAL_DATABASE_URL (опц., для rewrite-urls).
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ------------------------------------------------------------------ config
FRONTEND_ENV = Path("/Users/fayulloyadgarov/researcher-uz/.env.local")
FRONTEND_ENV_PUBLIC = Path("/Users/fayulloyadgarov/researcher-uz/.env")

BUCKETS = ("pdfs", "cover", "avatars")
PAGE = 1000

# (table, column) с image-URL — переписываются на R2. PDF-поля (articles.pdf,
# issues.full_pdf) намеренно НЕ здесь: их резолвит прокси.
IMAGE_COLUMNS = [
    ("profiles", "avatar_url"),
    ("publishers", "logo"),
    ("journals", "cover_image"),
    ("journals", "logo"),
    ("issues", "cover_image"),
    ("articles", "cover_image"),
]

try:
    import certifi  # type: ignore
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    _SSL_CTX = ssl._create_unverified_context()


def _read_env_file(path: Path, name: str) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith(name + "="):
            return line[len(name) + 1:].strip().strip('"')
    return None


def load_supabase() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL") or _read_env_file(
        FRONTEND_ENV_PUBLIC, "NEXT_PUBLIC_SUPABASE_URL"
    ) or _read_env_file(FRONTEND_ENV, "NEXT_PUBLIC_SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or _read_env_file(
        FRONTEND_ENV, "SUPABASE_SERVICE_ROLE_KEY"
    )
    if not url or not key:
        sys.exit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY не найдены")
    return url.rstrip("/"), key


def r2_client():
    """boto3-клиент R2 из настроек бэкенда (src.core.config)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.core.config import settings

    missing = [
        n for n in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
        if not getattr(settings, n)
    ]
    if missing:
        sys.exit(f"ERROR: не заданы R2-настройки: {', '.join(missing)}")

    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=settings.R2_ENDPOINT,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )
    return client, settings.R2_BUCKET, settings.R2_PUBLIC_BASE_URL


# ------------------------------------------------------------- Supabase Storage
def list_bucket(sb_url: str, key: str, bucket: str) -> list[dict]:
    """Список объектов бакета (пагинация). Возвращает [{name, size}]."""
    out: list[dict] = []
    offset = 0
    while True:
        req = urllib.request.Request(
            f"{sb_url}/storage/v1/object/list/{bucket}",
            method="POST",
            data=json.dumps(
                {
                    "prefix": "",
                    "limit": PAGE,
                    "offset": offset,
                    "sortBy": {"column": "name", "order": "asc"},
                }
            ).encode(),
        )
        req.add_header("apikey", key)
        req.add_header("Authorization", f"Bearer {key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
                batch = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            print(f"  ! list {bucket}: HTTP {e.code} {e.read().decode()[:200]}")
            break
        if not batch:
            break
        for o in batch:
            if not o.get("name"):
                continue
            meta = o.get("metadata") or {}
            size = int(meta.get("size") or 0)
            # Storage API отдаёт в листинге placeholder-ы папок: size=0 и без
            # metadata, а GET по ним возвращает 404. Реальных файлов нулевого
            # размера у нас нет, так что фильтруем — иначе copy вечно падает
            # на них, а verify показывает несуществующее расхождение.
            if not meta or size == 0:
                continue
            out.append({"name": o["name"], "size": size})
        if len(batch) < PAGE:
            break
        offset += PAGE
    return out


def download_with_retry(
    sb_url: str, key: str, bucket: str, name: str, attempts: int = 4
) -> bytes:
    """Скачивание с ретраями — сеть до Supabase периодически даёт read timeout,
    и один флапнувший файл не должен ронять прогон на 2300 объектов."""
    import time

    last: Exception | None = None
    for i in range(attempts):
        try:
            return download(sb_url, key, bucket, name)
        except Exception as e:  # noqa: BLE001 — таймауты/сетевые сбои
            last = e
            if i < attempts - 1:
                time.sleep(2 * (i + 1))  # 2s, 4s, 6s
    raise last  # type: ignore[misc]


def download(sb_url: str, key: str, bucket: str, name: str) -> bytes:
    # Имена объектов содержат пробелы/спецсимволы — кодируем путь (слэши папок
    # оставляем). R2-ключ храним ДЕКОДИРОВАННЫМ (`pdfs/<name с пробелами>`),
    # чтобы совпадать с key_from_url(unquote(...)) при чтении.
    from urllib.parse import quote

    req = urllib.request.Request(
        f"{sb_url}/storage/v1/object/{bucket}/{quote(name, safe='/')}"
    )
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=300, context=_SSL_CTX) as resp:
        return resp.read()


# ---------------------------------------------------------------------- modes
def mode_copy() -> None:
    sb_url, sb_key = load_supabase()
    client, r2_bucket, _ = r2_client()
    total = 0
    skipped = 0
    failed: list[str] = []
    for bucket in BUCKETS:
        objs = list_bucket(sb_url, sb_key, bucket)
        print(f"[{bucket}] объектов: {len(objs)}")
        for i, o in enumerate(objs, 1):
            r2_key = f"{bucket}/{o['name']}"
            # идемпотентность: пропускаем уже загруженное (=> прогон возобновляемый)
            try:
                client.head_object(Bucket=r2_bucket, Key=r2_key)
                skipped += 1
                continue
            except Exception:
                pass
            # один нескачавшийся файл не должен ронять весь прогон
            try:
                body = download_with_retry(sb_url, sb_key, bucket, o["name"])
                client.put_object(Bucket=r2_bucket, Key=r2_key, Body=body)
                total += 1
            except Exception as e:  # noqa: BLE001
                failed.append(f"{r2_key}: {type(e).__name__}")
                print(f"  ! пропущен {r2_key}: {type(e).__name__}")
            if i % 50 == 0:
                print(f"  ... {i}/{len(objs)}")
    print(f"Готово. Загружено новых: {total}, уже было: {skipped}, ошибок: {len(failed)}")
    if failed:
        print("Не удалось перенести:")
        for f in failed:
            print("  -", f)
        print("Повторный запуск `copy` дозальёт их (идемпотентно).")
        sys.exit(1)


def mode_verify() -> None:
    sb_url, sb_key = load_supabase()
    client, r2_bucket, _ = r2_client()
    ok = True
    for bucket in BUCKETS:
        objs = list_bucket(sb_url, sb_key, bucket)
        sb_count, sb_size = len(objs), sum(o["size"] for o in objs)
        # R2: считаем по префиксу
        paginator = client.get_paginator("list_objects_v2")
        r2_count = r2_size = 0
        for page in paginator.paginate(Bucket=r2_bucket, Prefix=f"{bucket}/"):
            for c in page.get("Contents", []):
                r2_count += 1
                r2_size += c.get("Size", 0)
        flag = "OK" if (sb_count == r2_count) else "MISMATCH"
        if sb_count != r2_count:
            ok = False
        print(f"[{bucket}] Supabase {sb_count} ({sb_size}b) vs R2 {r2_count} ({r2_size}b) -> {flag}")
    print("Сверка:", "OK" if ok else "ЕСТЬ РАСХОЖДЕНИЯ")
    if not ok:
        sys.exit(1)


def mode_rewrite_urls(dry_run: bool) -> None:
    import psycopg2

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.infrastructure.storage import key_from_url, public_url

    _, _, r2_base = r2_client()
    if not r2_base:
        sys.exit("ERROR: R2_PUBLIC_BASE_URL не задан — некуда переписывать URL")

    dsn = os.environ.get(
        "LOCAL_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/scientific_db",
    )
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    changed_total = 0
    with conn.cursor() as cur:
        for table, col in IMAGE_COLUMNS:
            cur.execute(
                f"SELECT id, {col} FROM public.{table} "
                f"WHERE {col} IS NOT NULL AND {col} <> ''"
            )
            rows = cur.fetchall()
            changed = 0
            for row_id, url in rows:
                key = key_from_url(url)
                new = public_url(key) if key else None
                if new and new != url:
                    if not dry_run:
                        cur.execute(
                            f"UPDATE public.{table} SET {col} = %s WHERE id = %s",
                            (new, row_id),
                        )
                    changed += 1
            changed_total += changed
            print(f"[{table}.{col}] к переписыванию: {changed}/{len(rows)}")
    if dry_run:
        conn.rollback()
        print(f"DRY-RUN: изменений не сохранено (всего было бы {changed_total}).")
    else:
        conn.commit()
        print(f"Готово. Переписано URL: {changed_total}.")
    conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    mode = args[0] if args else "copy"
    if mode == "copy":
        mode_copy()
    elif mode == "verify":
        mode_verify()
    elif mode == "rewrite-urls":
        mode_rewrite_urls("--dry-run" in args)
    else:
        sys.exit(f"Неизвестный режим: {mode} (copy | verify | rewrite-urls)")

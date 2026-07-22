"""Хранилище файлов — Cloudflare R2 (S3-совместимо), Фаза 6.

Заменяет Supabase Storage. Бакеты Supabase (`pdfs`, `cover`, `avatars`)
переезжают в один R2-бакет как префиксы ключей: `pdfs/<file>`, `cover/<file>`,
`avatars/<file>` — чтобы сохранить уникальные имена и раздачу по одному домену.

boto3 — синхронный клиент; операции оборачиваем в `run_in_threadpool`, чтобы не
блокировать event loop FastAPI. Клиент ленивый: без R2-кредов методы кидают
StorageNotConfigured (эндпоинты вернут 503), как OAuth-guard в Фазе 3.
"""
from __future__ import annotations

import mimetypes
from functools import lru_cache
from urllib.parse import quote, unquote, urlparse

from src.core.config import settings

# Известные бакеты Supabase → префиксы ключей в едином R2-бакете.
KNOWN_PREFIXES = ("pdfs", "cover", "avatars", "news")


class StorageNotConfigured(RuntimeError):
    """R2-креды не заданы (эндпоинт → 503)."""


@lru_cache(maxsize=1)
def _client():
    if not (
        settings.R2_ENDPOINT
        and settings.R2_ACCESS_KEY_ID
        and settings.R2_SECRET_ACCESS_KEY
        and settings.R2_BUCKET
    ):
        raise StorageNotConfigured(
            "R2 не сконфигурирован (R2_ACCOUNT_ID/ACCESS_KEY_ID/SECRET/BUCKET)"
        )
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.R2_ENDPOINT,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def public_url(key: str) -> str | None:
    """Публичный URL объекта по ключу (если задан R2_PUBLIC_BASE_URL).

    Ключ хранится декодированным (может содержать пробелы) — кодируем путь,
    чтобы URL был валиден (слэши префикса оставляем)."""
    base = settings.R2_PUBLIC_BASE_URL
    if not base:
        return None
    return f"{base.rstrip('/')}/{quote(key.lstrip('/'), safe='/')}"


def key_from_url(url: str | None, *, default_prefix: str | None = None) -> str | None:
    """Извлечь ключ объекта из сохранённого URL.

    Работает и с legacy Supabase public-URL (`/storage/v1/object/public/<bucket>/<file>`),
    и с R2 public-URL, и с голым именем файла. Возвращает `<prefix>/<file>` с
    ДЕКОДИРОВАННЫМ именем (совпадает с ключом объекта в R2, куда мы грузили сырое имя).
    """
    if not url:
        return None
    path = urlparse(url).path if "://" in url else url
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    filename = unquote(parts[-1])
    # Supabase: .../public/<bucket>/<file>
    if "public" in parts:
        i = parts.index("public")
        if i + 1 < len(parts):
            bucket = parts[i + 1]
            return f"{bucket}/{filename}"
    # Уже вида <prefix>/<file>
    if len(parts) >= 2 and parts[-2] in KNOWN_PREFIXES:
        return f"{parts[-2]}/{filename}"
    if default_prefix:
        return f"{default_prefix}/{filename}"
    return filename


class Storage:
    """Тонкая обёртка над R2 (S3). Синхронные методы — вызывать через
    `run_in_threadpool` из async-эндпоинтов."""

    @property
    def bucket(self) -> str:
        if not settings.R2_BUCKET:
            raise StorageNotConfigured("R2_BUCKET не задан")
        return settings.R2_BUCKET

    def put(self, key: str, content: bytes, content_type: str | None = None) -> str:
        ct = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"
        _client().put_object(Bucket=self.bucket, Key=key, Body=content, ContentType=ct)
        return key

    def get(self, key: str) -> tuple[bytes, str]:
        """Вернуть (body, content_type). KeyError, если объекта нет."""
        client = _client()
        try:
            obj = client.get_object(Bucket=self.bucket, Key=key)
        except client.exceptions.NoSuchKey as e:
            raise KeyError(key) from e
        return obj["Body"].read(), obj.get("ContentType", "application/octet-stream")

    def delete(self, key: str) -> None:
        _client().delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            _client().head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False


storage = Storage()

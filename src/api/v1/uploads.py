"""Загрузка/удаление файлов в R2 — порт `src/lib/storageUpload.ts` (Фаза 6).

Заменяет прямые `supabase.storage.from(bucket).upload()` из админ-форм. Доступ —
под сессией (get_current_profile); тонкую проверку владения статьёй делает
админ-форма под своим гейтом (как и во фронте). Имя файла — timestamped-слаг
базового имени под префиксом бакета (`pdfs/`, `cover/`, `avatars/`).
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from slugify import slugify

from src.api.deps import get_current_profile
from src.infrastructure.persistence.models import Profile
from src.infrastructure.storage import (
    KNOWN_PREFIXES,
    StorageNotConfigured,
    key_from_url,
    public_url,
    storage,
)

router = APIRouter()

# Мягкий лимит размера тела (PDF статьи может быть крупным).
_MAX_BYTES = 60 * 1024 * 1024  # 60 МБ


def _make_key(prefix: str, base_name: str, filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    slug = slugify(base_name) or "file"
    return f"{prefix}/{slug}-{int(time.time() * 1000)}.{ext}"


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    prefix: str = Form(...),
    base_name: str = Form("file"),
    _profile: Profile = Depends(get_current_profile),
):
    """Загрузить файл в R2. Возвращает {key, url}. `url` — публичный R2-URL
    (если задан R2_PUBLIC_BASE_URL), для картинок фронт хранит его напрямую;
    для PDF раздача идёт через /pdf/<slug> (ключ извлекается из url при чтении)."""
    if prefix not in KNOWN_PREFIXES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"prefix must be one of {KNOWN_PREFIXES}",
        )
    content = await file.read()
    if len(content) > _MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File too large")

    key = _make_key(prefix, base_name, file.filename or "file")
    try:
        await run_in_threadpool(storage.put, key, content, file.content_type)
    except StorageNotConfigured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Storage not configured")

    return {"key": key, "url": public_url(key) or key}


@router.delete("/by-url")
async def delete_by_url(
    url: str,
    _profile: Profile = Depends(get_current_profile),
):
    """Best-effort удаление объекта по сохранённому URL (порт removeStorageFileByUrl).
    Не падает, если объекта уже нет."""
    key = key_from_url(url)
    if not key:
        return {"deleted": False}
    try:
        await run_in_threadpool(storage.delete, key)
    except StorageNotConfigured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Storage not configured")
    except Exception:
        return {"deleted": False, "key": key}
    return {"deleted": True, "key": key}

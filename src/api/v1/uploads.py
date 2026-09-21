"""Загрузка/удаление файлов в R2 — порт `src/lib/storageUpload.ts` (Фаза 6).

Заменяет прямые `supabase.storage.from(bucket).upload()` из админ-форм. Доступ —
под сессией (get_current_profile); тонкую проверку владения статьёй делает
админ-форма под своим гейтом (как и во фронте). Имя файла — timestamped-слаг
базового имени под префиксом бакета (`pdfs/`, `cover/`, `avatars/`).
"""
from __future__ import annotations

import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from slugify import slugify
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_profile
from src.domain.authz import can_write_article, can_write_issue, is_owner
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import (
    Article,
    ImportItem,
    Issue,
    Journal,
    NewsPost,
    Profile,
    Publisher,
)
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


def _same_key(stored: str | None, key: str) -> bool:
    """Ссылается ли сохранённый URL на этот объект R2.

    В базе URL разных эпох (Supabase, R2, голый ключ), поэтому сравниваем через
    тот же разбор, что и при раздаче: `key_from_url` c префиксом ключа как
    умолчанием для старых записей без префикса.
    """
    if not stored:
        return False
    prefix = key.split("/", 1)[0] if "/" in key else None
    return key_from_url(stored, default_prefix=prefix) == key


async def _may_delete(db: AsyncSession, profile: Profile, key: str) -> bool:
    """Можно ли этому пользователю удалить объект `key`.

    Ручку зовут админ-формы при замене файла или отмене загрузки, и раньше она
    проверяла только наличие сессии: любой вошедший удалял по URL любой PDF или
    обложку на платформе. Теперь ищем строки, которые на файл ссылаются, и
    требуем право на КАЖДУЮ из них: статья — как на её правку, выпуск — как
    редактору журнала, аватар — только свой, журнал/издательство/новость/импорт
    — только владельцу. Файл, на который никто не ссылается (свежая загрузка
    из отменённой формы), удалять можно.
    """
    if is_owner(profile.role):
        return True
    filename = key.rsplit("/", 1)[-1]
    # Имя в URL может лежать и сырым, и percent-encoded — ищем оба варианта.
    needles = {f"%/{filename}", f"%/{quote(filename)}", filename}

    def like_any(*cols):
        return or_(*(col.like(n) for col in cols for n in needles))

    articles = (
        await db.execute(
            select(Article).where(like_any(Article.pdf, Article.cover_image))
        )
    ).scalars().all()
    for a in articles:
        if not (_same_key(a.pdf, key) or _same_key(a.cover_image, key)):
            continue
        if not await can_write_article(
            db,
            role=profile.role,
            user_id=profile.id,
            issue_id=a.issue_id,
            admin_id=a.admin_id,
            publisher_id=a.publisher_id,
        ):
            return False

    issues = (
        await db.execute(
            select(Issue).where(like_any(Issue.full_pdf, Issue.cover_image))
        )
    ).scalars().all()
    for i in issues:
        if not (_same_key(i.full_pdf, key) or _same_key(i.cover_image, key)):
            continue
        if not await can_write_issue(
            db, role=profile.role, user_id=profile.id, journal_id=i.journal_id
        ):
            return False

    publishers = (
        await db.execute(select(Publisher).where(like_any(Publisher.logo)))
    ).scalars().all()
    for pub in publishers:
        if _same_key(pub.logo, key) and pub.admin_id != profile.id:
            return False

    profiles = (
        await db.execute(select(Profile).where(like_any(Profile.avatar_url)))
    ).scalars().all()
    for pr in profiles:
        if _same_key(pr.avatar_url, key) and pr.id != profile.id:
            return False

    # Журналы, новости и импорт правит только владелец — ему ответили выше.
    journals = (
        await db.execute(
            select(Journal).where(like_any(Journal.cover_image, Journal.logo))
        )
    ).scalars().all()
    if any(_same_key(j.cover_image, key) or _same_key(j.logo, key) for j in journals):
        return False
    news = (
        await db.execute(select(NewsPost).where(like_any(NewsPost.cover_image)))
    ).scalars().all()
    if any(_same_key(n.cover_image, key) for n in news):
        return False
    items = (
        await db.execute(select(ImportItem).where(like_any(ImportItem.pdf_url)))
    ).scalars().all()
    if any(_same_key(it.pdf_url, key) for it in items):
        return False
    return True


@router.delete("/by-url")
async def delete_by_url(
    url: str,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    """Best-effort удаление объекта по сохранённому URL (порт removeStorageFileByUrl).
    Не падает, если объекта уже нет."""
    key = key_from_url(url)
    if not key:
        return {"deleted": False}
    if not await _may_delete(db, profile, key):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed to delete this file")
    try:
        await run_in_threadpool(storage.delete, key)
    except StorageNotConfigured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Storage not configured")
    except Exception:
        return {"deleted": False, "key": key}
    return {"deleted": True, "key": key}

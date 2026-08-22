"""Импорт архивов с других платформ (`/import`).

Всё под сессией и правами на журнал: задача привязана к `journal_id`, и каждая
ручка проверяет право писать в этот журнал тем же `can_write_issue`, что и
выпуски (owner или админ с записью в `journal_admins`). Чужую задачу не видно
и не применить.

Применение идёт в BackgroundTasks: состояние живёт в БД (`import_jobs.status`,
`heartbeat_at`), поэтому рестарт контейнера не теряет прогресс — задача
останется в `applying` с протухшей отметкой, и её продолжают тем же `apply`.
"""
from __future__ import annotations

import time

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from slugify import slugify
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_profile
from src.domain.authz import can_write_issue
from src.domain.importing import (
    ImportDomain,
    ImportError_,
    JobBusy,
    JobNotFound,
    template_csv,
)
from src.infrastructure.persistence.db import AsyncSessionLocal, get_db
from src.infrastructure.persistence.models import ImportJob, Profile
from src.infrastructure.storage import StorageNotConfigured, public_url, storage
from src.schemas.importing import (
    ImportApplyRequest,
    ImportItemPublic,
    ImportItemsPage,
    ImportItemUpdate,
    ImportJobCreate,
    ImportJobPublic,
    ImportParseResult,
    ImportPdfResult,
)

router = APIRouter()
domain = ImportDomain()

# Таблица метаданных крупной не бывает; PDF грузятся по одному тем же потолком,
# что и в /uploads (60 МБ) — архивом их слать нельзя, см. import-integration.md.
MAX_TABLE_BYTES = 15 * 1024 * 1024
MAX_PDF_BYTES = 60 * 1024 * 1024


def _forbidden() -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed to import into this journal")


async def _guard_journal(db: AsyncSession, profile: Profile, journal_id: int) -> None:
    if not await can_write_issue(
        db, role=profile.role, user_id=profile.id, journal_id=journal_id
    ):
        raise _forbidden()


async def _guarded_job(db: AsyncSession, profile: Profile, job_id: int) -> ImportJob:
    try:
        job = await domain.get_job(db, job_id)
    except JobNotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    await _guard_journal(db, profile, job.journal_id)
    return job


# --------------------------------------------------------------------- шаблон

@router.get("/template.csv")
async def download_template():
    """Шаблон таблицы. Без авторизации: это статический текст, не данные."""
    return Response(
        content=template_csv().encode("utf-8-sig"),  # BOM — чтобы Excel не ломал кириллицу
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="researcher-import-template.csv"'},
    )


# --------------------------------------------------------------------- задачи

@router.post("/jobs", response_model=ImportJobPublic, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: ImportJobCreate,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    await _guard_journal(db, profile, body.journal_id)
    if body.source_type != "table":
        # Источник OAI придёт этапом 3 (import-integration.md).
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Пока поддерживается только source_type=table")
    job = await domain.create_job(
        db,
        journal_id=body.journal_id,
        created_by=profile.id,
        source_type=body.source_type,
        source_ref=body.source_ref,
        params=body.params,
    )
    return job


@router.get("/jobs", response_model=list[ImportJobPublic])
async def list_jobs(
    journal_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    await _guard_journal(db, profile, journal_id)
    return await domain.list_jobs(db, journal_id)


@router.get("/jobs/{job_id}", response_model=ImportJobPublic)
async def get_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    return await _guarded_job(db, profile, job_id)


@router.post("/jobs/{job_id}/table", response_model=ImportParseResult)
async def upload_table(
    job_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    """Загрузить CSV/XLSX и разобрать его в кандидаты."""
    job = await _guarded_job(db, profile, job_id)
    content = await file.read()
    if len(content) > MAX_TABLE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Файл слишком большой")
    try:
        result = await domain.load_table(db, job, content, file.filename or "table.csv")
    except JobBusy as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    except ImportError_ as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    await db.refresh(job)
    return {"job": job, **result}


@router.post("/jobs/{job_id}/files", response_model=ImportPdfResult)
async def upload_pdf(
    job_id: int,
    file: UploadFile = File(...),
    filename: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    """Загрузить один PDF и привязать его к строке по имени файла.

    По одному файлу, а не архивом: 60-мегабайтный потолок тела и таймауты не
    оставляют шанса ZIP-у на триста статей, а пофайловая загрузка даёт прогресс
    и бесплатную докачку после разрыва.
    """
    job = await _guarded_job(db, profile, job_id)
    content = await file.read()
    if len(content) > MAX_PDF_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Файл слишком большой")
    # Проверяем, что это действительно PDF: иначе в хранилище приедет HTML-мусор
    # под видом статьи.
    if not content.startswith(b"%PDF"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Это не PDF-файл")

    original = filename or file.filename or "file.pdf"
    stem = original.rsplit(".", 1)[0]
    key = f"pdfs/{slugify(stem) or 'file'}-{int(time.time() * 1000)}.pdf"
    try:
        await run_in_threadpool(storage.put, key, content, "application/pdf")
    except StorageNotConfigured as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))

    url = public_url(key) or key
    item = await domain.attach_pdf(db, job, filename=original, url=url)
    return {"matched": item is not None, "item_id": item.id if item else None, "url": url}


@router.get("/jobs/{job_id}/items", response_model=ImportItemsPage)
async def list_items(
    job_id: int,
    item_status: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    await _guarded_job(db, profile, job_id)
    items, total = await domain.items(
        db, job_id, status=item_status, limit=limit, offset=offset
    )
    return {"items": items, "total": total}


@router.patch("/items/{item_id}", response_model=ImportItemPublic)
async def update_item(
    item_id: int,
    body: ImportItemUpdate,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    """Правка кандидата до применения (или снятие галочки статусом skipped)."""
    from sqlalchemy import select

    from src.infrastructure.persistence.models import ImportItem

    item = (
        await db.execute(select(ImportItem).where(ImportItem.id == item_id))
    ).scalars().first()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Строка импорта не найдена")
    await _guarded_job(db, profile, item.job_id)
    try:
        return await domain.set_item(db, item_id, parsed=body.parsed, status=body.status)
    except ImportError_ as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.post("/jobs/{job_id}/revalidate", response_model=ImportJobPublic)
async def revalidate(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    """Пересчитать дубликаты и сводку (после правок или дозагрузки файлов)."""
    job = await _guarded_job(db, profile, job_id)
    await domain.revalidate(db, job)
    await db.refresh(job)
    return job


async def _apply_in_background(job_id: int) -> None:
    """Фоновое применение — со СВОЕЙ сессией.

    Сессия запроса закрывается вместе с ответом, поэтому переиспользовать её в
    фоне нельзя: задача упала бы на первом же обращении к БД.
    """
    async with AsyncSessionLocal() as db:
        job = await domain.get_job(db, job_id)
        try:
            await domain.apply(db, job)
        except Exception:
            # apply уже записал failed/error в задачу; наверх пробрасывать
            # некому — фон никто не ждёт.
            pass


@router.post("/jobs/{job_id}/apply", response_model=ImportJobPublic)
async def apply_job(
    job_id: int,
    body: ImportApplyRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    """Запустить импорт. Возвращает задачу сразу — прогресс опрашивается GET-ом.

    С явным `item_ids` применяем синхронно (это выбранные вручную строки, их
    немного), без него — в фоне: архив на сотни статей не должен держать
    HTTP-запрос.
    """
    job = await _guarded_job(db, profile, job_id)
    if job.status == "applying" and not domain._is_stale(job):
        raise HTTPException(status.HTTP_409_CONFLICT, "Импорт уже идёт")

    if body.item_ids:
        try:
            await domain.apply(db, job, body.item_ids)
        except ImportError_ as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
        await db.refresh(job)
        return job

    background.add_task(_apply_in_background, job.id)
    job.status = "applying"
    await db.commit()
    await db.refresh(job)
    return job


@router.post("/jobs/{job_id}/cancel", response_model=ImportJobPublic)
async def cancel_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    job = await _guarded_job(db, profile, job_id)
    await domain.cancel(db, job)
    await db.refresh(job)
    return job

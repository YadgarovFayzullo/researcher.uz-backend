"""Импорт архивов с других платформ (`/import`).

Доступ — ТОЛЬКО владелец платформы: импорт продаётся как услуга, редактор
журнала его не запускает. Поэтому здесь `require_owner`, а не привычный для
выпусков `can_write_issue`; `journal_id` в задаче остаётся — он говорит, в
какой журнал лить, но правом доступа больше не является.

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

from src.api.deps import require_owner
from src.domain.importing import (
    ImportDomain,
    ImportError_,
    JobBusy,
    JobNotFound,
    template_csv,
)
from src.infrastructure.external.oai import (
    OaiError,
    base_url_from_site,
    identify,
    list_sets,
    set_hint_from_site,
)
from src.infrastructure.external.safe_fetch import BlockedAddress, FetchError
from src.infrastructure.persistence.db import AsyncSessionLocal, get_db
from src.infrastructure.persistence.models import ImportJob, Journal, Profile
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
    OaiDiscoverRequest,
    OaiDiscoverResult,
    OaiParseRequest,
)

router = APIRouter()
domain = ImportDomain()

# Таблица метаданных крупной не бывает; PDF грузятся по одному тем же потолком,
# что и в /uploads (60 МБ) — архивом их слать нельзя, см. import-integration.md.
MAX_TABLE_BYTES = 15 * 1024 * 1024
MAX_PDF_BYTES = 60 * 1024 * 1024


async def _require_journal(db: AsyncSession, journal_id: int) -> None:
    """Журнал должен существовать: импорт в несуществующий id — опечатка."""
    exists = (
        await db.execute(select(Journal.id).where(Journal.id == journal_id))
    ).scalars().first()
    if exists is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Журнал не найден")


async def _get_job(db: AsyncSession, job_id: int) -> ImportJob:
    try:
        return await domain.get_job(db, job_id)
    except JobNotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))


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
    profile: Profile = Depends(require_owner),
):
    await _require_journal(db, body.journal_id)
    if body.source_type not in ("table", "oai"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "source_type должен быть 'table' или 'oai'"
        )
    if body.source_type == "oai" and not (body.params or {}).get("base_url"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Для импорта со старого сайта нужен params.base_url"
        )
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
    profile: Profile = Depends(require_owner),
):
    return await domain.list_jobs(db, journal_id)


@router.get("/jobs/{job_id}", response_model=ImportJobPublic)
async def get_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(require_owner),
):
    return await _get_job(db, job_id)


@router.post("/jobs/{job_id}/table", response_model=ImportParseResult)
async def upload_table(
    job_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(require_owner),
):
    """Загрузить CSV/XLSX и разобрать его в кандидаты."""
    job = await _get_job(db, job_id)
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
    profile: Profile = Depends(require_owner),
):
    """Загрузить один PDF и привязать его к строке по имени файла.

    По одному файлу, а не архивом: 60-мегабайтный потолок тела и таймауты не
    оставляют шанса ZIP-у на триста статей, а пофайловая загрузка даёт прогресс
    и бесплатную докачку после разрыва.
    """
    job = await _get_job(db, job_id)
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


@router.post("/discover", response_model=OaiDiscoverResult)
async def discover_repository(
    body: OaiDiscoverRequest,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(require_owner),
):
    """Что за сайт по этому адресу и какие журналы на нём есть.

    Под правами на журнал, а не под простой сессией: ручка ходит по адресу,
    который назвал пользователь, и открывать такой инструмент всем подряд не
    стоит даже с SSRF-фильтром.
    """
    await _require_journal(db, body.journal_id)
    try:
        base_url = base_url_from_site(body.site_url)
        info = await identify(base_url)
        sets = await list_sets(base_url)
    except OaiError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except BlockedAddress as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except FetchError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    # Если клиент вставил ссылку на конкретный журнал, подставляем его в
    # выборе — но только если такой сет действительно есть в репозитории.
    hint = set_hint_from_site(body.site_url)
    known = {s.spec for s in sets}
    return {
        "base_url": base_url,
        "repository_name": info.get("name", ""),
        "sets": [{"spec": s.spec, "name": s.name} for s in sets],
        "suggested_set": hint if hint in known else None,
    }


async def _parse_oai_in_background(job_id: int, resume: bool = False) -> None:
    """Обход репозитория в фоне — со своей сессией (сессия запроса уже закрыта).

    Разбор архива на тысячу статей идёт минуты: держать на нём HTTP-запрос
    нельзя, а состояние и так живёт в задаче.
    """
    async with AsyncSessionLocal() as db:
        job = await domain.get_job(db, job_id)
        try:
            await domain.load_oai(db, job, resume=resume)
        except Exception as e:
            job.status = "failed"
            job.error = str(e)[:500]
            await db.commit()


@router.post("/jobs/{job_id}/oai", response_model=ImportJobPublic)
async def start_oai_parse(
    job_id: int,
    body: OaiParseRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(require_owner),
):
    """Запустить обход репозитория. Прогресс — обычным GET задачи."""
    job = await _get_job(db, job_id)
    if job.status in ("parsing", "applying"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Задача уже обрабатывается")

    params = dict(job.params or {})
    if body.resume and not params.get("resume_token"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Этот архив уже забран целиком — продолжать нечего"
        )
    if body.set_spec:
        params["set"] = body.set_spec
    if body.date_from:
        params["from"] = body.date_from
    if body.date_until:
        params["until"] = body.date_until
    job.params = params
    job.status = "parsing"
    job.error = None
    job.source_ref = params.get("base_url")
    await db.commit()

    background.add_task(_parse_oai_in_background, job.id, body.resume)
    await db.refresh(job)
    return job


@router.get("/jobs/{job_id}/items", response_model=ImportItemsPage)
async def list_items(
    job_id: int,
    item_status: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(require_owner),
):
    await _get_job(db, job_id)
    items, total = await domain.items(
        db, job_id, status=item_status, limit=limit, offset=offset
    )
    return {"items": items, "total": total}


@router.patch("/items/{item_id}", response_model=ImportItemPublic)
async def update_item(
    item_id: int,
    body: ImportItemUpdate,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(require_owner),
):
    """Правка кандидата до применения (или снятие галочки статусом skipped)."""
    from sqlalchemy import select

    from src.infrastructure.persistence.models import ImportItem

    item = (
        await db.execute(select(ImportItem).where(ImportItem.id == item_id))
    ).scalars().first()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Строка импорта не найдена")
    try:
        return await domain.set_item(db, item_id, parsed=body.parsed, status=body.status)
    except ImportError_ as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.post("/jobs/{job_id}/revalidate", response_model=ImportJobPublic)
async def revalidate(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(require_owner),
):
    """Пересчитать дубликаты и сводку (после правок или дозагрузки файлов)."""
    job = await _get_job(db, job_id)
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
    profile: Profile = Depends(require_owner),
):
    """Запустить импорт. Возвращает задачу сразу — прогресс опрашивается GET-ом.

    С явным `item_ids` применяем синхронно (это выбранные вручную строки, их
    немного), без него — в фоне: архив на сотни статей не должен держать
    HTTP-запрос.
    """
    job = await _get_job(db, job_id)
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
    profile: Profile = Depends(require_owner),
):
    job = await _get_job(db, job_id)
    await domain.cancel(db, job)
    await db.refresh(job)
    return job

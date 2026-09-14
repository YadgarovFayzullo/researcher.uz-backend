"""Загрузка папки PDF редактором выпуска (`/import/folder`).

Импорт архивов (`imports.py`) — платная услуга владельца. Здесь другое: рабочий
инструмент редактора. Он бросает в свой выпуск папку со статьями, а система
сама читает из каждого PDF название, авторов, аннотацию, ключевые слова и
страницы (`src/domain/pdf_metadata.py`), заводит статьи и отправляет их на ту
же автопроверку, что и статьи из формы (`src/domain/ai_review.py`). Поэтому и
права те же, что на выпуск: owner или админ, привязанный к журналу.

Мгновенной публикации нет сознательно. Папку чужих статей в свой номер
редактор уже однажды заливал (см. `moderation.py`), и путь «папкой» не должен
обходить проверку, которую прошла бы та же статья из формы. При выключенной
автопроверке (`ISSUE_REVIEW_ENABLED=false`) статьи публикуются сразу — ровно
как из формы.

Состояние живёт в тех же `import_jobs` / `import_items`: загрузка рвётся (сотня
файлов, модель отвечает секундами), и продолжать её надо с места обрыва.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_profile
from src.core.config import settings
from src.domain import pdf_metadata
from src.domain.authz import can_write_issue
from src.domain.importing import (
    FOLDER_STALE_AFTER_SECONDS,
    ImportDomain,
    ImportError_,
    JobBusy,
    JobNotFound,
    extract_status_of,
    item_public,
)
from src.infrastructure.persistence.db import AsyncSessionLocal, get_db
from src.infrastructure.persistence.models import ImportItem, ImportJob, Issue, Profile
from src.infrastructure.storage import StorageNotConfigured, public_url, storage
from src.schemas.importing import (
    FolderFileResult,
    FolderJobCreate,
    ImportItemPublic,
    ImportItemsPage,
    ImportItemUpdate,
    ImportJobPublic,
)

logger = logging.getLogger(__name__)

router = APIRouter()
domain = ImportDomain()

# Как в /uploads и импорте архивов: крупнее PDF статьи не бывает.
MAX_PDF_BYTES = 60 * 1024 * 1024

# Что редактор правит в строке до создания статьи. Год, том и номер — от
# выпуска, DOI присваивает платформа, поэтому их здесь нет. `confirmed` —
# «проверил, всё верно»: сверки с текстом файла становятся предупреждениями.
EDITABLE_FIELDS = {
    "title",
    "title_foreign",
    "authors",
    "annotation",
    "annotation_foreign",
    "keywords",
    "keywords_foreign",
    "pages",
    "confirmed",
}


async def _guard(db: AsyncSession, profile: Profile, journal_id: int) -> None:
    if not await can_write_issue(
        db, role=profile.role, user_id=profile.id, journal_id=journal_id
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет прав на выпуски этого журнала")


async def _job(db: AsyncSession, profile: Profile, job_id: int) -> ImportJob:
    try:
        job = await domain.get_job(db, job_id)
    except JobNotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    # Задачи импорта архивов — владельца, через эти ручки их не видно.
    if job.source_type != "folder":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Загрузка не найдена")
    await _guard(db, profile, job.journal_id)
    return job


def _busy(job: ImportJob) -> bool:
    return job.status in ("parsing", "applying") and not domain._is_stale(
        job, FOLDER_STALE_AFTER_SECONDS
    )


def _claim(job: ImportJob, state: str) -> None:
    """Занять задачу до ухода в фон: второй клик не запустит второй прогон."""
    job.status = state
    job.error = None
    job.finished_at = None
    job.heartbeat_at = datetime.now(timezone.utc)


# --------------------------------------------------------------------- задачи


@router.post("/jobs", response_model=ImportJobPublic, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: FolderJobCreate,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    issue = (await db.execute(select(Issue).where(Issue.id == body.issue_id))).scalars().first()
    if issue is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Выпуск не найден")
    await _guard(db, profile, issue.journal_id)
    return await domain.create_job(
        db,
        journal_id=issue.journal_id,
        created_by=profile.id,
        source_type="folder",
        source_ref=f"issue:{issue.id}",
        # `review` — чтобы экран честно сказал, опубликуются статьи сразу или
        # после автопроверки.
        params={"issue_id": issue.id, "review": settings.REVIEW_ACTIVE},
    )


@router.get("/jobs/{job_id}", response_model=ImportJobPublic)
async def get_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    return await _job(db, profile, job_id)


@router.post("/jobs/{job_id}/files", response_model=FolderFileResult)
async def upload_file(
    job_id: int,
    file: UploadFile = File(...),
    filename: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    """Принять один PDF из папки.

    По одному файлу, а не архивом — как в импорте архивов: 60-мегабайтный
    потолок тела не пропустит папку выпуска, а пофайловая загрузка даёт
    прогресс и докачку после разрыва. Модель здесь не зовётся — только текст
    первых страниц и regex, поэтому ответ приходит сразу.
    """
    job = await _job(db, profile, job_id)
    if _busy(job):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Файлы этой загрузки уже распознаются — начните новую загрузку",
        )
    content = await file.read()
    if len(content) > MAX_PDF_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Файл больше 60 МБ")
    if not content.startswith(b"%PDF"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Это не PDF-файл")

    # Имя с путём внутри папки («2024-3/12_karimov.pdf») — по нему редактор
    # узнаёт файл в списке.
    original = (filename or file.filename or "file.pdf").strip()[-300:]
    try:
        existing = await domain.folder_precheck(db, job, content)
    except ImportError_ as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    if existing is not None:
        return {"item": item_public(existing, extract_status_of(existing)), "duplicate": True}

    try:
        head = await run_in_threadpool(
            pdf_metadata.read_head, content, max_pages=settings.IMPORT_EXTRACT_PAGES
        )
    except pdf_metadata.PdfUnreadable:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Файл повреждён: PDF не открывается")

    stem = original.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    key = f"pdfs/{slugify(stem)[:60] or 'article'}-{int(time.time() * 1000)}.pdf"
    try:
        await run_in_threadpool(storage.put, key, content, "application/pdf")
    except StorageNotConfigured as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))

    try:
        item = await domain.add_folder_file(
            db, job, filename=original, content=content, url=public_url(key) or key, head=head
        )
    except (JobBusy, ImportError_) as e:
        # Файл уже в хранилище, а строки нет — не оставляем сироту.
        try:
            await run_in_threadpool(storage.delete, key)
        except Exception:
            logger.warning("folder import: не удалось удалить %s после отказа", key)
        code = status.HTTP_409_CONFLICT if isinstance(e, JobBusy) else status.HTTP_400_BAD_REQUEST
        raise HTTPException(code, str(e))
    if item.pdf_url != (public_url(key) or key):
        # Строку успела завести параллельная загрузка того же файла — наш
        # экземпляр в хранилище больше ни на что не ссылается.
        try:
            await run_in_threadpool(storage.delete, key)
        except Exception:
            logger.warning("folder import: не удалось удалить дубликат %s", key)
        return {"item": item_public(item, extract_status_of(item)), "duplicate": True}
    return {"item": item_public(item, extract_status_of(item)), "duplicate": False}


async def _run(job_id: int, *, extract: bool) -> None:
    """Распознать файлы и создать статьи — в фоне, со своей сессией.

    Сессия запроса закрывается вместе с ответом; состояние живёт в задаче,
    поэтому рестарт контейнера посреди прогона лечится повторным нажатием.
    """
    async with AsyncSessionLocal() as db:
        try:
            job = await domain.get_job(db, job_id)
            if extract:
                await domain.extract_folder(db, job)
            await domain.apply(db, job, claimed=True)
        except Exception as e:
            logger.exception("folder import %s: прогон упал", job_id)
            await db.rollback()
            job = await domain.get_job(db, job_id)
            job.status = "failed"
            job.error = str(e)[:500]
            await db.commit()
            return

        # Обложки — как у статьи из формы. Рендер занимает секунды на файл,
        # поэтому после создания всех статей, а не между ними.
        from src.api.v1.article import _fill_cover

        for article_id in await domain.created_article_ids(db, job_id):
            try:
                await _fill_cover(article_id)
            except Exception:
                logger.exception("folder import %s: обложка статьи %s", job_id, article_id)


@router.post("/jobs/{job_id}/process", response_model=ImportJobPublic)
async def process_job(
    job_id: int,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    """Распознать принятые файлы и создать статьи. Прогресс — GET задачи."""
    job = await _job(db, profile, job_id)
    if _busy(job):
        raise HTTPException(status.HTTP_409_CONFLICT, "Загрузка уже обрабатывается")
    _claim(job, "parsing")
    await db.commit()
    background.add_task(_run, job.id, extract=True)
    await db.refresh(job)
    return job


@router.post("/jobs/{job_id}/apply", response_model=ImportJobPublic)
async def apply_job(
    job_id: int,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    """Создать статьи по исправленным строкам. Модель второй раз не зовётся."""
    job = await _job(db, profile, job_id)
    if _busy(job):
        raise HTTPException(status.HTTP_409_CONFLICT, "Загрузка уже обрабатывается")
    _claim(job, "applying")
    await db.commit()
    background.add_task(_run, job.id, extract=False)
    await db.refresh(job)
    return job


@router.get("/jobs/{job_id}/items", response_model=ImportItemsPage)
async def list_items(
    job_id: int,
    item_status: str | None = Query(None, alias="status"),
    limit: int = Query(300, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    await _job(db, profile, job_id)
    items, total = await domain.folder_items(
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
    """Поправить строку до создания статьи или пропустить её."""
    item = (
        await db.execute(select(ImportItem).where(ImportItem.id == item_id))
    ).scalars().first()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Строка загрузки не найдена")
    job = await _job(db, profile, item.job_id)
    if _busy(job):
        # Фон пишет в те же строки — правка посреди прогона потерялась бы.
        raise HTTPException(status.HTTP_409_CONFLICT, "Дождитесь окончания обработки")
    if body.status not in (None, "pending", "skipped"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Строку можно только пропустить или вернуть"
        )
    parsed = (
        {k: v for k, v in body.parsed.items() if k in EDITABLE_FIELDS}
        if body.parsed is not None
        else None
    )
    try:
        await domain.set_item(db, item_id, parsed=parsed, status=body.status)
    except ImportError_ as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    # Дубли и пересечения страниц — свойство всей загрузки, а не строки.
    await domain.revalidate(db, job)
    await db.refresh(item)
    return item_public(item, extract_status_of(item))

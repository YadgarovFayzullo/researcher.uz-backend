"""Проверка на заимствования (`/plagiarism`).

Доступ — владелец платформы: как и импорт, это платная услуга, а не бесплатная
кнопка в панели журнала. Когда появится тарификация, гейт можно ослабить до
журнальных админов с лимитами — доменный слой к этому готов, проверка не
завязана на роль.

Считается по базе платформы (`src/domain/plagiarism.py`). Что это ловит и чего
не ловит — в докстринге домена; интерфейс обязан показывать это честно, иначе
редактор примет 0% за «в интернете тоже чисто».
"""
from __future__ import annotations

import asyncio

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import require_owner
from src.domain.plagiarism import PlagiarismDomain, PlagiarismError
from src.infrastructure.pdf_text import pdf_to_checkable_text
from src.infrastructure.persistence.db import AsyncSessionLocal, get_db
from src.infrastructure.persistence.models import Article, ArticleText, Issue, Profile
from src.infrastructure.storage import StorageNotConfigured, key_from_url, storage
from src.schemas.plagiarism import (
    PlagiarismCheckPublic,
    PlagiarismCoverage,
    PlagiarismReport,
)

router = APIRouter()
domain = PlagiarismDomain()

MAX_PDF_BYTES = 60 * 1024 * 1024


async def _run_in_background(check_id: int, text: str) -> None:
    """Считаем в фоне со своей сессией — сессия запроса закрыта вместе с ответом."""
    async with AsyncSessionLocal() as db:
        check = await domain.get_check(db, check_id)
        if check is None:
            return
        try:
            await domain.run(db, check, text)
        except Exception:
            # run уже записал failed и текст ошибки в саму проверку.
            pass


@router.get("/coverage", response_model=PlagiarismCoverage)
async def coverage(
    _owner: Profile = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Сколько статей проиндексировано — размер базы сравнения."""
    return await domain.coverage(db)


@router.post("/checks", response_model=PlagiarismCheckPublic, status_code=status.HTTP_201_CREATED)
async def check_file(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    journal_id: int | None = Form(None),
    owner: Profile = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Проверить присланный PDF (рукопись, ещё не заведённую в системе)."""
    content = await file.read()
    if len(content) > MAX_PDF_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Файл слишком большой")
    if not content.startswith(b"%PDF"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нужен PDF-файл")

    text = pdf_to_checkable_text(content)
    check = await domain.create_check(
        db, journal_id=journal_id, created_by=owner.id, title=file.filename
    )
    background.add_task(_run_in_background, check.id, text)
    return check


@router.post(
    "/checks/article/{article_id}",
    response_model=PlagiarismCheckPublic,
    status_code=status.HTTP_201_CREATED,
)
async def check_article(
    article_id: int,
    background: BackgroundTasks,
    owner: Profile = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Проверить статью, уже заведённую в системе.

    Текст берём из индекса, если он там есть; иначе достаём PDF из хранилища —
    чтобы проверка работала и для статей, до которых индексация ещё не дошла.
    """
    article = (
        await db.execute(select(Article).where(Article.id == article_id))
    ).scalars().first()
    if article is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Статья не найдена")

    stored = (
        await db.execute(select(ArticleText).where(ArticleText.article_id == article_id))
    ).scalars().first()
    text = stored.content if stored and stored.status == "ok" else ""

    if not text:
        if not article.pdf:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "У статьи нет PDF-файла")
        key = key_from_url(article.pdf, default_prefix="pdfs")
        try:
            body, _ = await asyncio.to_thread(storage.get, key)
        except StorageNotConfigured as e:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
        except Exception:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Не удалось получить PDF статьи")
        text = pdf_to_checkable_text(body)

    journal_id = None
    if article.issue_id:
        issue = (
            await db.execute(select(Issue).where(Issue.id == article.issue_id))
        ).scalars().first()
        journal_id = issue.journal_id if issue else None

    check = await domain.create_check(
        db,
        journal_id=journal_id,
        created_by=owner.id,
        title=article.title,
        article_id=article_id,
    )
    background.add_task(_run_in_background, check.id, text)
    return check


@router.get("/checks", response_model=list[PlagiarismCheckPublic])
async def list_checks(
    journal_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    _owner: Profile = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    return await domain.list_checks(db, journal_id=journal_id, limit=limit)


@router.get("/checks/{check_id}", response_model=PlagiarismReport)
async def get_report(
    check_id: int,
    _owner: Profile = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    check = await domain.get_check(db, check_id)
    if check is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Проверка не найдена")
    matches = await domain.matches(db, check_id) if check.status == "done" else []
    return {"check": check, "matches": matches}

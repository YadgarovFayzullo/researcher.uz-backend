"""Раздача PDF — порт `src/app/pdf/[filename]/route.ts` (Фаза 6, R2).

researcher.uz/pdf/<slug>.pdf проксирует файл из R2, чтобы полный текст лежал на
том же домене, что и страница статьи (требование Google Scholar). Ключ объекта
извлекается из сохранённого `articles.pdf` (любой формат URL) — БД не переписываем.
"""
from __future__ import annotations

import base64
import re

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from src.domain.article import ArticleDomain
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Issue
from src.infrastructure.storage import StorageNotConfigured, key_from_url, storage

router = APIRouter()
domain = ArticleDomain()

# Content-Disposition защищаем от инъекции: слаг всегда [a-z0-9-], но чистим на
# всякий случай (кавычки/CR/LF нельзя протаскивать в заголовок).
_SAFE_SLUG = re.compile(r"[^a-zA-Z0-9._-]")

_CACHE = (
    "public, max-age=0, must-revalidate, s-maxage=86400, "
    "stale-while-revalidate=604800"
)


async def _stream_object(
    request: Request, *, key: str | None, download_name: str
) -> Response:
    """Отдать объект хранилища с поддержкой Range, ETag и 304.

    Вынесено из `serve_pdf`, потому что сборник выпуска раздаётся точно так же:
    просмотрщик запрашивает файл кусками, и без Range он тянул бы десятки
    мегабайт ради первой страницы.
    """
    if not key:
        return Response("Not found", status_code=404)

    etag = '"' + base64.urlsafe_b64encode(key.encode()).decode()[:32] + '"'
    headers = {
        "Content-Type": "application/pdf",
        "Content-Disposition": f'inline; filename="{download_name}"',
        "Cache-Control": _CACHE,
        "ETag": etag,
        "Accept-Ranges": "bytes",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    try:
        body, _, content_range, total = await run_in_threadpool(
            storage.get_range, key, request.headers.get("range")
        )
    except StorageNotConfigured:
        return Response("Storage not configured", status_code=503)
    except KeyError:
        return Response("Not found", status_code=404)
    except ValueError:
        return Response(
            "Range not satisfiable", status_code=416, headers={"Accept-Ranges": "bytes"}
        )
    except Exception:
        return Response("Upstream error", status_code=502)

    headers["Content-Length"] = str(len(body))
    if content_range:
        headers["Content-Range"] = content_range
        return Response(content=body, status_code=206, headers=headers)
    if total:
        headers["Content-Length"] = str(total)
    return Response(content=body, status_code=200, headers=headers)


@router.get("/pdf/issue/{issue_id}")
async def serve_issue_pdf(
    issue_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """Сборник выпуска целиком — один PDF со всеми статьями номера.

    Отдельная ручка, а не общий /pdf/<slug>: у выпуска нет слуга, а раздавать
    файл прямой ссылкой на хранилище не хочется — тогда адрес объекта уходит
    наружу и его нельзя ни поменять, ни посчитать.

    Объявлена ВЫШЕ /pdf/{filename}: иначе путь «issue/12» съел бы общий
    обработчик и искал бы статью со слугом «issue».
    """
    clean_id = re.sub(r"\.pdf$", "", issue_id, flags=re.IGNORECASE)
    if not clean_id.isdigit():
        return Response("Not found", status_code=404)

    issue = (
        await db.execute(select(Issue).where(Issue.id == int(clean_id)))
    ).scalars().first()
    if not issue or not issue.full_pdf:
        return Response("Not found", status_code=404)

    name_parts = [p for p in (
        f"vol{issue.volume}" if issue.volume else None,
        f"no{issue.issue}" if issue.issue else None,
        str(issue.year) if issue.year else None,
    ) if p]
    filename = _SAFE_SLUG.sub("", "-".join(name_parts) or f"issue-{clean_id}")

    return await _stream_object(
        request,
        key=key_from_url(issue.full_pdf, default_prefix="pdfs"),
        download_name=f"{filename}.pdf",
    )


@router.get("/pdf/{filename}")
async def serve_pdf(filename: str, request: Request, db: AsyncSession = Depends(get_db)):
    slug = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
    safe_slug = _SAFE_SLUG.sub("", slug)

    # Снятая с публикации статья не должна раздавать и PDF.
    article = await domain.get_article_by_slug(db, slug, published_only=True)
    if not article or not article.pdf:
        return Response("Not found", status_code=404)

    return await _stream_object(
        request,
        key=key_from_url(article.pdf, default_prefix="pdfs"),
        download_name=f"{safe_slug}.pdf",
    )

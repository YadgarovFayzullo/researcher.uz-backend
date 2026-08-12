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

from src.domain.article import ArticleDomain
from src.infrastructure.persistence.db import get_db
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


@router.get("/pdf/{filename}")
async def serve_pdf(filename: str, request: Request, db: AsyncSession = Depends(get_db)):
    slug = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
    safe_slug = _SAFE_SLUG.sub("", slug)

    # Снятая с публикации статья не должна раздавать и PDF.
    article = await domain.get_article_by_slug(db, slug, published_only=True)
    if not article or not article.pdf:
        return Response("Not found", status_code=404)

    key = key_from_url(article.pdf, default_prefix="pdfs")
    # ETag завязан на URL/ключ файла — меняется только при замене PDF.
    etag = '"' + base64.urlsafe_b64encode(article.pdf.encode()).decode()[:32] + '"'
    headers = {
        "Content-Type": "application/pdf",
        "Content-Disposition": f'inline; filename="{safe_slug}.pdf"',
        "Cache-Control": _CACHE,
        "ETag": etag,
        # Без Accept-Ranges pdf.js даже не пытается запрашивать куски и тянет
        # файл целиком — на странице издателя это десятки мегабайт ради обложек.
        "Accept-Ranges": "bytes",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    range_header = request.headers.get("range")

    try:
        body, _, content_range, total = await run_in_threadpool(
            storage.get_range, key, range_header
        )
    except StorageNotConfigured:
        return Response("Storage not configured", status_code=503)
    except KeyError:
        return Response("Not found", status_code=404)
    except ValueError:
        # Диапазон вне размера файла.
        return Response(
            "Range not satisfiable",
            status_code=416,
            headers={"Accept-Ranges": "bytes"},
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

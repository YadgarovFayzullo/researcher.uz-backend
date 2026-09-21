"""Депонирование статьи в Zenodo — выдача внешнего DOI.

Порт `src/lib/uploadToZenodo.ts` фронта. Раньше форма ходила в Zenodo прямо из
браузера с токеном из `NEXT_PUBLIC_ZENODO_TOKEN`: переменная с таким префиксом
вшивается в клиентский бандл, и токен аккаунта Zenodo мог забрать любой
посетитель — а с ним депонировать и удалять что угодно от имени платформы.
Теперь токен живёт в `ZENODO_TOKEN` бэкенда, а сюда приходит файл с
метаданными от вошедшего редактора.

Шаги те же, что были во фронте: черновик → файл → метаданные → publish.
Черновик Zenodo сразу после создания иногда отвечает 403 на загрузку файла,
поэтому каждый шаг повторяется с растущей паузой.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from src.api.deps import get_current_profile
from src.core.config import settings
from src.infrastructure.persistence.models import Profile

router = APIRouter()
logger = logging.getLogger(__name__)

_MAX_BYTES = 60 * 1024 * 1024
_RETRIES = 5
_FIRST_DELAY = 2.0


class _ZenodoError(Exception):
    def __init__(self, message: str, http_status: int = status.HTTP_502_BAD_GATEWAY):
        super().__init__(message)
        self.http_status = http_status


def _require_config() -> str:
    if not settings.ZENODO_TOKEN:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Zenodo is not configured (set ZENODO_TOKEN)",
        )
    return settings.ZENODO_TOKEN


async def _retry(op, *, retries: int = _RETRIES, delay: float = _FIRST_DELAY):
    for attempt in range(1, retries + 1):
        try:
            return await op()
        except httpx.HTTPError as exc:
            if attempt == retries:
                raise
            logger.info("Zenodo: попытка %s не удалась (%s), ждём %.0fс", attempt, exc, delay)
            await asyncio.sleep(delay)
            delay *= 2


def _raise_for(resp: httpx.Response, step: str) -> None:
    if resp.is_success:
        return
    detail: Any = None
    try:
        detail = resp.json()
    except ValueError:
        detail = resp.text[:300]
    logger.warning("Zenodo %s: HTTP %s %s", step, resp.status_code, detail)
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        raise _ZenodoError(
            "Достигнут лимит запросов к Zenodo, попробуйте позже",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )
    if isinstance(detail, dict) and isinstance(detail.get("errors"), list):
        parts = [
            f"{e.get('field') or 'unknown'}: {e.get('message')}"
            for e in detail["errors"]
            if isinstance(e, dict)
        ]
        raise _ZenodoError(f"Zenodo ({step}): " + ", ".join(parts))
    resp.raise_for_status()


def _normalize_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    if not meta.get("title"):
        raise _ZenodoError("В метаданных нет обязательного поля title", status.HTTP_400_BAD_REQUEST)
    if not meta.get("description"):
        raise _ZenodoError(
            "В метаданных нет обязательного поля description", status.HTTP_400_BAD_REQUEST
        )
    creators = meta.get("creators")
    if not isinstance(creators, list) or not creators:
        raise _ZenodoError("В метаданных нет списка creators", status.HTTP_400_BAD_REQUEST)
    meta.setdefault("upload_type", "publication")
    meta.setdefault("publication_type", "article")
    meta.setdefault("access_right", "open")
    return meta


async def _deposit(token: str, filename: str, content: bytes, meta: dict[str, Any]) -> str:
    base = settings.ZENODO_API_BASE.rstrip("/")
    auth = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:

        async def create():
            r = await client.post(f"{base}/deposit/depositions", json={}, headers=auth)
            _raise_for(r, "create")
            return r.json()

        deposit = await _retry(create)
        dep_id = deposit["id"]
        await asyncio.sleep(1.5)

        async def upload():
            r = await client.post(
                f"{base}/deposit/depositions/{dep_id}/files",
                files={"file": (filename, content, "application/pdf")},
                headers=auth,
            )
            _raise_for(r, "upload")

        await _retry(upload)

        async def put_meta():
            r = await client.put(
                f"{base}/deposit/depositions/{dep_id}",
                json={"metadata": meta},
                headers=auth,
            )
            _raise_for(r, "metadata")

        await _retry(put_meta)

        async def publish():
            r = await client.post(
                f"{base}/deposit/depositions/{dep_id}/actions/publish", headers=auth
            )
            _raise_for(r, "publish")
            return r.json()

        published = await _retry(publish)
        doi = published.get("doi")
        if not doi:
            raise _ZenodoError("Zenodo опубликовал запись, но не вернул DOI")
        return doi


@router.post("/deposit")
async def deposit_article(
    file: UploadFile = File(...),
    metadata: str = Form(..., description="JSON метаданных депозита Zenodo"),
    profile: Profile = Depends(get_current_profile),
):
    """Загрузить PDF в Zenodo и вернуть {doi}. Только редакторам и владельцу —
    форма статьи и так открыта им одним, а у обычного аккаунта права
    депонировать от имени платформы нет."""
    if profile.role not in ("owner", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed to deposit")
    token = _require_config()
    try:
        meta = json.loads(metadata)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "metadata must be JSON")
    if not isinstance(meta, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "metadata must be an object")

    content = await file.read()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty file")
    if len(content) > _MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File too large")

    try:
        doi = await _deposit(token, file.filename or "article.pdf", content, _normalize_metadata(meta))
    except _ZenodoError as exc:
        raise HTTPException(exc.http_status, str(exc))
    except httpx.HTTPError as exc:
        logger.warning("Zenodo недоступен: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Zenodo request failed")
    logger.info("Zenodo: DOI %s выдан пользователем %s", doi, profile.id)
    return {"doi": doi}

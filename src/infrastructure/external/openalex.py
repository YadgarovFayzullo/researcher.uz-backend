"""Интеграция с OpenAlex (https://openalex.org) — порт `src/lib/openalex.ts`.

Бесплатный открытый индекс работ/цитирований, без API-ключа. Тянем счётчик
цитирований (cited_by_count) и разбивку по годам (counts_by_year) по DOI.
Используется owner-роутом обновления кэша external_citations.
"""
from __future__ import annotations

import re

import httpx

from src.core.config import settings

OPENALEX_BASE = "https://api.openalex.org/works"
_DOI_PREFIX = re.compile(r"^https?://(dx\.)?doi\.org/", re.IGNORECASE)
_DOI_SCHEME = re.compile(r"^doi:", re.IGNORECASE)


def normalize_doi(doi: str | None) -> str:
    """DOI → канон: нижний регистр, без https://doi.org/ и doi:."""
    if not doi:
        return ""
    d = doi.strip().lower()
    d = _DOI_PREFIX.sub("", d)
    d = _DOI_SCHEME.sub("", d)
    return d.strip()


async def fetch_citations_by_dois(dois: list[str | None]) -> dict[str, dict]:
    """Батч-счётчики цитирований по DOI. Возвращает {norm_doi: {cited_by_count,
    counts_by_year}}. Ошибка одного батча не роняет остальные (как в TS-версии).
    """
    clean = sorted({d for d in (normalize_doi(x) for x in dois) if d})
    result: dict[str, dict] = {}
    if not clean:
        return result

    CHUNK = 50
    async with httpx.AsyncClient(timeout=30.0) as client:
        for i in range(0, len(clean), CHUNK):
            chunk = clean[i : i + CHUNK]
            params = {
                "filter": f"doi:{'|'.join(chunk)}",
                "per-page": CHUNK,
                "select": "doi,cited_by_count,counts_by_year",
                "mailto": settings.OPENALEX_MAILTO,
            }
            try:
                res = await client.get(
                    OPENALEX_BASE, params=params, headers={"Accept": "application/json"}
                )
                if res.status_code != 200:
                    continue
                json = res.json()
            except (httpx.HTTPError, ValueError):
                continue

            for w in json.get("results") or []:
                doi = normalize_doi(w.get("doi"))
                if not doi:
                    continue
                counts = []
                for c in w.get("counts_by_year") or []:
                    try:
                        year = int(c.get("year"))
                    except (TypeError, ValueError):
                        continue
                    counts.append(
                        {"year": year, "count": int(c.get("cited_by_count") or 0)}
                    )
                result[doi] = {
                    "cited_by_count": int(w.get("cited_by_count") or 0),
                    "counts_by_year": counts,
                }
    return result

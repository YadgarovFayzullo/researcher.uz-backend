"""Импорт сборников выпусков со старого сайта журнала (OJS).

Статьи переносит `/import` (см. import-integration.md), но у выпуска на OJS
есть ещё «Full Issue» — весь номер одним файлом. Он не приходит ни в OAI, ни
со страницы статьи: его надо брать со страницы выпуска.

Что делает скрипт: обходит архив выпусков журнала, для каждого читает
заголовок вида «Vol. 1 No. 5 (2023)», находит ссылку Full Issue, скачивает
файл в наше хранилище и проставляет `issues.full_pdf` тому выпуску нашей
платформы, у которого совпали год, том и номер.

Идемпотентно: выпуски с уже заполненным `full_pdf` пропускаются (`--force`
перезаписывает). Сеть идёт через safe_fetch — те же SSRF-проверки и вежливый
троттлинг в один запрос к чужому сайту в секунду.

    PYTHONPATH=. python scripts/import_issue_pdfs.py \\
        --journal 17 --site https://universalpublishings.com/index.php/jusr --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from urllib.parse import urljoin

from sqlalchemy import select

from src.infrastructure.external.safe_fetch import FetchError, fetch
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import Issue
from src.infrastructure.storage import StorageNotConfigured, public_url, storage

MAX_PAGE_BYTES = 5 * 1024 * 1024
# Сборник номера — это все статьи одним файлом, он куда тяжелее отдельной
# статьи: у выпуска на 268 работ легко набирается сотня мегабайт.
MAX_ISSUE_PDF_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_PAGES = 20

_ISSUE_LINK = re.compile(r"/issue/view/(\d+)(?![\d/])")
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
# «Vol. 1 No. 5 (2023)» — так OJS подписывает выпуск; том и номер бывают без
# префиксов в переводах, поэтому цифры ищем и по отдельности.
_VOLUME = re.compile(r"(?:vol\.?|volume|том|jild)\s*([0-9]+)", re.I)
_NUMBER = re.compile(r"(?:no\.?|num\.?|number|№|son)\s*([0-9]+)", re.I)
_YEAR = re.compile(r"\((\d{4})\)")
_FULL_ISSUE_BLOCK = re.compile(
    r"full\s*issue|butun\s*son|to['’ʻ`]?liq\s*son|весь\s*выпуск", re.I
)
_DOWNLOAD_LINK = re.compile(r'href="([^"]*/issue/(?:download|view)/\d+/\d+)"', re.I)


async def get(url: str, *, max_bytes: int = MAX_PAGE_BYTES) -> bytes:
    return (await fetch(url, max_bytes=max_bytes, accept="text/html")).content


async def collect_issue_urls(site: str) -> list[str]:
    """Адреса всех выпусков журнала из архива (архив бывает многостраничным)."""
    seen: dict[str, None] = {}
    for page in range(1, MAX_ARCHIVE_PAGES + 1):
        url = f"{site.rstrip('/')}/issue/archive" + ("" if page == 1 else f"/{page}")
        try:
            html = (await get(url)).decode("utf-8", "replace")
        except FetchError:
            break
        found = _ISSUE_LINK.findall(html)
        if not found:
            break
        before = len(seen)
        for issue_id in found:
            seen.setdefault(f"{site.rstrip('/')}/issue/view/{issue_id}", None)
        if len(seen) == before:
            # Страница ничего не добавила — дальше идёт повтор, а не архив.
            break
    return list(seen)


def parse_issue_label(html: str) -> tuple[int | None, str | None, str | None]:
    """Со страницы выпуска: (год, том, номер)."""
    for raw in _H1.findall(html)[:3]:
        label = " ".join(_TAGS.sub(" ", raw).split())
        year = _YEAR.search(label)
        volume = _VOLUME.search(label)
        number = _NUMBER.search(label)
        if year or volume or number:
            return (
                int(year.group(1)) if year else None,
                volume.group(1) if volume else None,
                number.group(1) if number else None,
            )
    return None, None, None


def find_full_issue_link(html: str, base_url: str) -> str | None:
    """Ссылка на файл всего номера, если он выложен."""
    match = _FULL_ISSUE_BLOCK.search(html)
    if not match:
        return None
    # Ищем ближайшую ссылку на galley выпуска после подписи «Full Issue».
    tail = html[match.start() : match.start() + 4000]
    link = _DOWNLOAD_LINK.search(tail) or _DOWNLOAD_LINK.search(html)
    if not link:
        return None
    return urljoin(base_url, link.group(1).replace("/issue/view/", "/issue/download/"))


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--journal", type=int, required=True, help="id журнала у нас")
    p.add_argument("--site", required=True, help="адрес журнала на OJS")
    p.add_argument("--force", action="store_true", help="перезаписать уже заполненные")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    started = time.time()
    urls = await collect_issue_urls(args.site)
    print(f"выпусков на сайте: {len(urls)}")

    async with AsyncSessionLocal() as db:
        ours = list(
            (
                await db.execute(select(Issue).where(Issue.journal_id == args.journal))
            ).scalars().all()
        )
        by_key = {
            (i.year, (i.volume or "").strip(), (i.issue or "").strip()): i for i in ours
        }
        print(f"выпусков у нас: {len(ours)}")

        matched = uploaded = skipped = missing = 0
        for url in urls:
            try:
                html = (await get(url)).decode("utf-8", "replace")
            except FetchError as e:
                print(f"  ! {url}: {e}")
                continue

            year, volume, number = parse_issue_label(html)
            key = (year, volume or "", number or "")
            issue = by_key.get(key)
            if issue is None:
                continue
            matched += 1

            if issue.full_pdf and not args.force:
                skipped += 1
                continue

            link = find_full_issue_link(html, url)
            if not link:
                missing += 1
                print(f"  — {year}/{volume}-{number}: сборника нет на сайте")
                continue

            if args.dry_run:
                print(f"  = {year}/{volume}-{number}: нашёлся сборник → {link}")
                continue

            try:
                result = await fetch(
                    link, max_bytes=MAX_ISSUE_PDF_BYTES, accept="application/pdf"
                )
            except FetchError as e:
                print(f"  ! {year}/{volume}-{number}: {e}")
                continue
            if not result.content.startswith(b"%PDF"):
                print(f"  ! {year}/{volume}-{number}: по ссылке не PDF")
                continue

            key_name = f"pdfs/issue-{args.journal}-{year or 'x'}-{volume or 'x'}-{number or 'x'}-{int(time.time())}.pdf"
            try:
                await asyncio.to_thread(
                    storage.put, key_name, result.content, "application/pdf"
                )
            except StorageNotConfigured as e:
                print("Хранилище не настроено:", e)
                return 1

            issue.full_pdf = public_url(key_name) or key_name
            await db.commit()
            uploaded += 1
            print(
                f"  + {year}/{volume}-{number}: {len(result.content) // 1024} КБ → выпуск {issue.id}"
            )

        print(
            f"\nИтог: совпало {matched}, загружено {uploaded}, "
            f"пропущено (уже есть) {skipped}, без сборника {missing}, "
            f"за {time.time() - started:.0f}с"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

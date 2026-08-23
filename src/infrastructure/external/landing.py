"""Страница статьи на старом сайте: Highwire-теги и ссылка на PDF.

Зачем отдельный шаг, если есть OAI: в `oai_dc` нет файла (там `dc:relation`
ведёт на HTML-страницу galley, а не на PDF) и обычно нет DOI. На landing page
OJS отдаёт полный набор Highwire-тегов — `citation_pdf_url`, `citation_doi`,
`citation_author` (по тегу на автора), точные страницы, том и номер. Проверено
на живом OJS 2026-08-22.

Шаг дорогой (запрос на статью + запрос на файл), поэтому выполняется только
для записей, которые клиент действительно импортирует.

HTML разбираем регуляркой по <meta>, а не парсером: тянуть bs4/lxml ради
десятка тегов незачем, а structure-aware разбор здесь ничего не даёт — теги
плоские и лежат в <head>.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from src.infrastructure.external.safe_fetch import FetchError, fetch

MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 60 * 1024 * 1024

# <meta name="citation_title" content="..."> в любом порядке атрибутов и с
# любыми кавычками. Ограничиваем name цитатными тегами, чтобы не тащить всё.
_META = re.compile(
    r"""<meta\s+(?=[^>]*\bname\s*=\s*["']?(?P<name>citation_[a-z_]+)["']?)"""
    r"""(?=[^>]*\bcontent\s*=\s*(?P<q>["'])(?P<value>.*?)(?P=q))[^>]*>""",
    re.IGNORECASE | re.DOTALL,
)
# Запасной путь: PDF-ссылка прямо в разметке (OJS зовёт её .../download/...).
_DOWNLOAD_HREF = re.compile(
    r"""href\s*=\s*["'](?P<url>[^"']*/article/download/[^"']+)["']""", re.IGNORECASE
)


@dataclass
class LandingData:
    """Метаданные со страницы статьи. Пустые поля — норма, не ошибка."""

    title: str | None = None
    doi: str | None = None
    authors: list[str] = field(default_factory=list)
    date: str | None = None
    volume: str | None = None
    issue: str | None = None
    firstpage: str | None = None
    lastpage: str | None = None
    journal_title: str | None = None
    language: str | None = None
    keywords: list[str] = field(default_factory=list)
    pdf_url: str | None = None

    @property
    def pages(self) -> str | None:
        if self.firstpage and self.lastpage:
            return f"{self.firstpage}-{self.lastpage}"
        return self.firstpage or None


def parse_landing_html(page: str, *, base_url: str) -> LandingData:
    data = LandingData()
    tags: dict[str, list[str]] = {}
    for match in _META.finditer(page):
        name = match.group("name").lower()
        value = html.unescape(match.group("value") or "").strip()
        if value:
            tags.setdefault(name, []).append(value)

    def first(name: str) -> str | None:
        values = tags.get(name)
        return values[0] if values else None

    data.title = first("citation_title")
    data.doi = first("citation_doi")
    data.authors = tags.get("citation_author", [])
    data.date = first("citation_publication_date") or first("citation_date")
    data.volume = first("citation_volume")
    data.issue = first("citation_issue")
    data.firstpage = first("citation_firstpage")
    data.lastpage = first("citation_lastpage")
    data.journal_title = first("citation_journal_title")
    data.language = first("citation_language")
    data.keywords = tags.get("citation_keywords", [])

    pdf = first("citation_pdf_url")
    if not pdf:
        # Тега нет — берём ссылку на скачивание из разметки. На OJS она ведёт
        # на тот же файл, только без Highwire-обёртки.
        match = _DOWNLOAD_HREF.search(page)
        if match:
            pdf = html.unescape(match.group("url"))
    if pdf and pdf.startswith("/"):
        # Относительная ссылка — достраиваем от адреса страницы.
        from urllib.parse import urljoin

        pdf = urljoin(base_url, pdf)
    data.pdf_url = pdf
    return data


async def fetch_landing(url: str) -> LandingData:
    """Скачать страницу статьи и вытащить из неё метаданные."""
    result = await fetch(url, max_bytes=MAX_PAGE_BYTES, accept="text/html")
    page = result.content.decode("utf-8", errors="replace")
    return parse_landing_html(page, base_url=result.url)


async def fetch_pdf(url: str) -> bytes:
    """Скачать PDF статьи.

    На OJS `citation_pdf_url` часто ведёт не на файл, а на страницу-обёртку
    просмотрщика; настоящий файл лежит на ссылке `/article/download/...`,
    которую эта страница и содержит. Поэтому один раз пробуем «провалиться»
    глубже, а не отдаём клиенту HTML под видом статьи.
    """
    result = await fetch(url, max_bytes=MAX_PDF_BYTES, accept="application/pdf")
    if result.content.startswith(b"%PDF"):
        return result.content

    if result.content_type in ("text/html", "application/xhtml+xml"):
        page = result.content.decode("utf-8", errors="replace")
        match = _DOWNLOAD_HREF.search(page)
        if match:
            from urllib.parse import urljoin

            deeper = urljoin(result.url, html.unescape(match.group("url")))
            if deeper != result.url:
                inner = await fetch(deeper, max_bytes=MAX_PDF_BYTES, accept="application/pdf")
                if inner.content.startswith(b"%PDF"):
                    return inner.content

    raise FetchError(
        f"По ссылке {url} лежит не PDF"
        + (f" (это {result.content_type})" if result.content_type else "")
    )

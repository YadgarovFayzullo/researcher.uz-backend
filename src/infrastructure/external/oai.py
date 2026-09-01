"""OAI-PMH: перечисление и разбор записей старого сайта журнала.

Проверено на живом PKP/OJS (inlibrary.uz, 2026-08-22) — на нём сидит большая
часть узбекских журналов. Что важно знать про реальные ответы:

* Точка входа у OJS — `<site>/index.php/index/oai`; журналы перечислены в
  `ListSets`, где `setSpec` = код журнала (`cpis`), а вложенный `cpis:ST` —
  его разделы. Импортируем по коду журнала.
* `ListRecords` отдаёт страницами по 100 с `resumptionToken`; в общем потоке
  первые сотни записей бывают `<header status="deleted">` — их пропускаем.
* Метаданные многоязычные, с `xml:lang`: `dc:title` en/ru/uz и т.д. Кладём в
  нашу пару «основной / иностранный».
* `dc:source` = «Название журнала; Vol. 1 No. 1 (2025); 5-7» — отсюда том,
  номер, год и страницы.
* PDF в OAI НЕТ: `dc:relation` ведёт на HTML-страницу galley. Настоящий файл
  достаётся с landing page (`dc:identifier`) через `citation_pdf_url` — там же
  лежат DOI, авторы и точные страницы. Этим занимается `landing.py`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse
from xml.etree import ElementTree as ET

from src.infrastructure.external.safe_fetch import FetchError, fetch

NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}

# Ответ OAI — это XML со списком записей; сотня записей с аннотациями на трёх
# языках укладывается в единицы мегабайт.
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
# Потолок обхода: пагинация по resumptionToken теоретически бесконечна, а
# зациклившийся репозиторий не должен занять обработчик навсегда. Это нижняя
# граница: размер страницы задаёт репозиторий, и он бывает крошечным
# (КиберЛенинка отдаёт по 10 записей, OJS — по 100), поэтому реальный бюджет
# страниц считается от запрошенного объёма — см. `list_records`. Без этого
# обход архива в 2400 работ обрывался на 600-й.
MAX_PAGES = 60
# Жёсткий предел на случай репозитория, который отдаёт токен без записей.
MAX_PAGES_HARD = 5000


# Управляющие символы, запрещённые в XML 1.0 (кроме tab/LF/CR). В UTF-8 они
# однобайтовые, а все байты многобайтовых последовательностей ≥ 0x80 — значит
# такую чистку можно делать прямо по байтам, не разрушая кириллицу.
_CONTROL_CHARS = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class OaiError(Exception):
    """Репозиторий ответил ошибкой или чем-то, что не разобрать."""


def _error_context(content: bytes, error: ET.ParseError) -> str:
    """Кусок ответа вокруг места ошибки — иначе диагностировать нечем."""
    line, column = getattr(error, "position", (0, 0))
    lines = content.split(b"\n")
    if not (0 < line <= len(lines)):
        return ""
    fragment = lines[line - 1][max(0, column - 60) : column + 40]
    return "Фрагмент: …" + fragment.decode("utf-8", "replace") + "…"


@dataclass
class OaiSet:
    spec: str
    name: str


@dataclass
class OaiRecord:
    identifier: str
    datestamp: str | None = None
    titles: dict[str, str] = field(default_factory=dict)      # lang → значение
    # Авторы приходят по разу на каждый язык записи («Рахматуллаев, М» и
    # «Raxmatullayev, M» — один человек), поэтому храним их по языкам и берём
    # один список, а не склеиваем все варианты в толпу однофамильцев.
    creators: dict[str, list[str]] = field(default_factory=dict)
    subjects: dict[str, list[str]] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    source: str | None = None
    landing_url: str | None = None
    date: str | None = None
    language: str | None = None
    rights: list[str] = field(default_factory=list)
    publisher: str | None = None
    doi: str | None = None


def base_url_from_site(raw: str) -> str:
    """Из адреса сайта журнала вывести точку входа OAI.

    Клиент вводит что угодно: `inlibrary.uz`, `https://inlibrary.uz/index.php/cpis`
    или сразу `.../index/oai`. Приводим к `<site>/index.php/index/oai` — общей
    для всего OJS-инстанса, потому что журнал выбирается сетом, а не URL-ом.
    """
    value = (raw or "").strip()
    if not value:
        raise OaiError("Не указан адрес сайта")
    if not value.startswith(("http://", "https://")):
        value = "https://" + value

    parsed = urlparse(value)
    path = parsed.path.rstrip("/")
    if path.endswith("/oai"):
        return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
    return urlunparse((parsed.scheme, parsed.netloc, "/index.php/index/oai", "", "", ""))


def set_hint_from_site(raw: str) -> str | None:
    """Код журнала из ссылки, если клиент вставил её целиком.

    `https://inlibrary.uz/index.php/cpis` и `.../index.php/cpis/issue/archive`
    → `cpis`. Это подсказка для выбора журнала в списке, а не источник истины:
    совпадение с реальным `setSpec` проверяется по ответу `ListSets`.
    """
    value = (raw or "").strip()
    if not value:
        return None
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    parts = [p for p in urlparse(value).path.split("/") if p]
    if "index.php" not in parts:
        return None
    tail = parts[parts.index("index.php") + 1 :]
    if not tail:
        return None
    code = tail[0]
    # `index` — служебный путь самого OJS, а не журнал.
    return None if code in ("index", "oai") else code


async def _request(base_url: str, params: dict[str, str]) -> ET.Element:
    url = f"{base_url}?{urlencode(params)}"
    try:
        result = await fetch(url, max_bytes=MAX_RESPONSE_BYTES, accept="application/xml")
    except FetchError as e:
        raise OaiError(str(e)) from e

    try:
        root = ET.fromstring(result.content)
    except ET.ParseError as first_error:
        # Реальные архивы содержат символы, недопустимые в XML вообще: в
        # universalpublishings.com попался сырой 0x02 посреди заголовка статьи
        # (текст вставляли из Word). Такой ответ не спасёт ни один парсер, но
        # терять из-за одного байта сотню статей нельзя — вычищаем и пробуем
        # снова.
        cleaned = _CONTROL_CHARS.sub(b"", result.content)
        try:
            root = ET.fromstring(cleaned)
        except ET.ParseError:
            raise OaiError(
                f"Ответ {url} не удалось разобрать как XML "
                f"(получено {result.content_type or 'непонятно что'}): {first_error}. "
                f"{_error_context(result.content, first_error)}"
            ) from first_error

    error = root.find("oai:error", NS)
    if error is not None:
        code = error.get("code", "")
        text = (error.text or "").strip()
        raise OaiError(f"Репозиторий ответил ошибкой {code}: {text or 'без пояснения'}")
    return root


async def identify(base_url: str) -> dict[str, str]:
    """Проверка, что перед нами OAI-репозиторий, + его название."""
    root = await _request(base_url, {"verb": "Identify"})
    node = root.find("oai:Identify", NS)
    if node is None:
        raise OaiError("Ответ без блока Identify — это не OAI-репозиторий")
    return {
        "name": (node.findtext("oai:repositoryName", default="", namespaces=NS) or "").strip(),
        "base_url": (node.findtext("oai:baseURL", default="", namespaces=NS) or "").strip(),
        "granularity": (node.findtext("oai:granularity", default="", namespaces=NS) or "").strip(),
    }


async def list_sets(base_url: str) -> list[OaiSet]:
    """Журналы инстанса. Вложенные сеты (`code:SECTION`) отбрасываем —
    импортируют журнал целиком, а не отдельный раздел."""
    sets: list[OaiSet] = []
    token: str | None = None
    for _ in range(MAX_PAGES):
        params = {"verb": "ListSets"} if token is None else {"verb": "ListSets", "resumptionToken": token}
        root = await _request(base_url, params)
        container = root.find("oai:ListSets", NS)
        if container is None:
            break
        for node in container.findall("oai:set", NS):
            spec = (node.findtext("oai:setSpec", default="", namespaces=NS) or "").strip()
            name = (node.findtext("oai:setName", default="", namespaces=NS) or "").strip()
            if spec and ":" not in spec:
                sets.append(OaiSet(spec=spec, name=name or spec))
        token = _resumption_token(container)
        if not token:
            break
    return sets


def _resumption_token(container: ET.Element) -> str | None:
    node = container.find("oai:resumptionToken", NS)
    if node is None:
        return None
    token = (node.text or "").strip()
    return token or None


@dataclass
class RecordsPage:
    """Порция записей и место, с которого можно продолжить.

    Архивы бывают больше потолка одной задачи (у universalpublishings.com в
    одном журнале 3940 статей при потолке 2000), поэтому обход прерывается и
    возобновляется с сохранённого токена, а не начинается заново.
    """

    records: list[OaiRecord]
    resume_token: str | None = None   # None — архив пройден до конца
    total_in_repository: int | None = None


async def list_records(
    base_url: str,
    *,
    set_spec: str | None = None,
    date_from: str | None = None,
    date_until: str | None = None,
    limit: int = 2000,
    resume_token: str | None = None,
) -> RecordsPage:
    """Записи журнала. Удалённые пропускаем, служебные — тоже."""
    records: list[OaiRecord] = []
    params: dict[str, str] = {"verb": "ListRecords", "metadataPrefix": "oai_dc"}
    if set_spec:
        params["set"] = set_spec
    if date_from:
        params["from"] = date_from
    if date_until:
        params["until"] = date_until

    token: str | None = resume_token
    total: int | None = None
    # Каждая страница приносит хотя бы одну запись, поэтому `limit` страниц
    # заведомо хватает на `limit` записей при любом размере страницы.
    page_budget = min(max(MAX_PAGES, limit), MAX_PAGES_HARD)
    for _ in range(page_budget):
        # После первой страницы OAI требует ТОЛЬКО resumptionToken: verb с
        # набором параметров вместе с токеном репозиторий отвергает.
        page_params = params if token is None else {"verb": "ListRecords", "resumptionToken": token}
        root = await _request(base_url, page_params)
        container = root.find("oai:ListRecords", NS)
        if container is None:
            break

        node = container.find("oai:resumptionToken", NS)
        if node is not None and node.get("completeListSize"):
            try:
                total = int(node.get("completeListSize", ""))
            except ValueError:
                total = None

        for record_node in container.findall("oai:record", NS):
            header = record_node.find("oai:header", NS)
            if header is not None and header.get("status") == "deleted":
                continue
            metadata = record_node.find("oai:metadata", NS)
            if metadata is None:
                continue
            record = _parse_record(header, metadata)
            if record is not None:
                records.append(record)

        token = _resumption_token(container)
        if not token:
            return RecordsPage(records=records, resume_token=None, total_in_repository=total)
        # Потолок проверяем МЕЖДУ страницами, а не внутри: оборвав страницу
        # посередине, мы бы не знали, с какого места продолжать — токен
        # указывает на границу страницы, а не на запись.
        if len(records) >= limit:
            return RecordsPage(records=records, resume_token=token, total_in_repository=total)

    return RecordsPage(records=records, resume_token=token, total_in_repository=total)


def _lang_of(node: ET.Element) -> str:
    return (node.get("{http://www.w3.org/XML/1998/namespace}lang") or "").lower()


def _parse_record(header: ET.Element | None, metadata: ET.Element) -> OaiRecord | None:
    dc = metadata.find("oai_dc:dc", NS)
    if dc is None:
        return None

    identifier = ""
    datestamp = None
    if header is not None:
        identifier = (header.findtext("oai:identifier", default="", namespaces=NS) or "").strip()
        datestamp = (header.findtext("oai:datestamp", default="", namespaces=NS) or "").strip() or None

    record = OaiRecord(identifier=identifier, datestamp=datestamp)

    for node in dc:
        tag = node.tag.split("}")[-1]
        value = (node.text or "").strip()
        if not value:
            continue
        lang = _lang_of(node)

        if tag == "title":
            record.titles.setdefault(lang, value)
        elif tag == "creator":
            record.creators.setdefault(lang, []).append(value)
        elif tag == "subject":
            record.subjects.setdefault(lang, []).append(value)
        elif tag == "description":
            record.descriptions.setdefault(lang, value)
        elif tag == "source":
            record.source = record.source or value
        elif tag == "identifier":
            if value.lower().startswith("10.") or "doi.org/" in value.lower():
                record.doi = record.doi or value
            elif value.startswith("http") and record.landing_url is None:
                record.landing_url = value
        elif tag == "date":
            record.date = record.date or value
        elif tag == "language":
            record.language = record.language or value
        elif tag == "rights":
            record.rights.append(value)
        elif tag == "publisher":
            record.publisher = record.publisher or value

    return record


# --- разбор dc:source ------------------------------------------------------
#
# «Contemporary problems of intelligent systems; Vol. 1 No. 1 (2025); 5-7»
# Части разделены `;`: название журнала, координаты выпуска, страницы. Языковые
# варианты («Том 1 № 1 (2025)») отличаются только словами — цифры те же.
_VOLUME = re.compile(r"(?:vol\.?|volume|том|t\.|jild)\s*([0-9IVXLC]+)", re.IGNORECASE)
_NUMBER = re.compile(r"(?:no\.?|num\.?|number|№|son)\s*([0-9]+)", re.IGNORECASE)
_YEAR = re.compile(r"\((\d{4})\)")
_PAGES = re.compile(r"^\s*(\d+)\s*[-–—]\s*(\d+)\s*$")


def parse_source(source: str | None) -> dict[str, Any]:
    """`dc:source` → {journal_title, volume, issue, year, pages}."""
    out: dict[str, Any] = {}
    if not source:
        return out

    parts = [p.strip() for p in source.split(";") if p.strip()]
    if parts:
        out["journal_title"] = parts[0]

    for part in parts[1:]:
        pages = _PAGES.match(part)
        if pages:
            out["pages"] = f"{pages.group(1)}-{pages.group(2)}"
            continue
        volume = _VOLUME.search(part)
        if volume:
            out["volume"] = volume.group(1)
        number = _NUMBER.search(part)
        if number:
            out["issue"] = number.group(1)
        year = _YEAR.search(part)
        if year:
            out["year"] = int(year.group(1))
    return out

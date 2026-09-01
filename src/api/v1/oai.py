"""OAI-PMH 2.0 — выдача метаданных допущенным харвестерам (закрыта ключом).

baseURL: https://api.researcher.uz/oai?key=<выданный ключ>

Реализованы все шесть глаголов протокола; формат метаданных — oai_dc (он
обязателен по спецификации и его достаточно для BASE/CORE/OpenAIRE). Для EBSCO
позже сюда добавляется второй prefix (JATS) — точка расширения одна:
`_METADATA_FORMATS` и функция рендера записи.

Отличие от «классического» OAI одно и намеренное: без ключа эндпоинт отвечает
401, а не пустым списком. Кто такой ключ получает и как он проверяется —
src/core/oai_access.py.

Ошибки самого протокола (badVerb, badArgument, …) возвращаются, как велит
спецификация, с HTTP 200 и элементом <error> внутри — харвестеры разбирают
именно его, а не код ответа.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.oai_access import TOKEN_QUERY_PARAM, authenticate
from src.core.ratelimit import limiter
from src.domain.oai import (
    GRANULARITY,
    OaiDomain,
    OaiError,
    Scope,
    Selector,
    decode_token,
    encode_token,
    format_datestamp,
    parse_datestamp,
)
from src.infrastructure.persistence.db import get_db

router = APIRouter()
domain = OaiDomain()

OAI_NS = "http://www.openarchives.org/OAI/2.0/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
OAI_DC_NS = "http://www.openarchives.org/OAI/2.0/oai_dc/"
DC_NS = "http://purl.org/dc/elements/1.1/"
ID_NS = "http://www.openarchives.org/OAI/2.0/oai-identifier"

ET.register_namespace("", OAI_NS)
ET.register_namespace("xsi", XSI_NS)
ET.register_namespace("oai_dc", OAI_DC_NS)
ET.register_namespace("dc", DC_NS)

_METADATA_FORMATS = {
    "oai_dc": (OAI_DC_NS, "http://www.openarchives.org/OAI/2.0/oai_dc.xsd"),
}

# oai:researcher.uz:article/123
_IDENTIFIER_RE = re.compile(r"^oai:(?P<ns>[^:]+):article/(?P<id>\d+)$")

# Разделители в свободных полях (keywords, authors) — в базе встречаются оба.
_SPLIT_RE = re.compile(r"\s*[;,]\s*")


# --------------------------------------------------------------- служебное

def _base_url(request: Request) -> str:
    if settings.OAI_BASE_URL:
        return settings.OAI_BASE_URL.rstrip("/")
    return f"{request.url.scheme}://{request.url.netloc}{request.url.path}".rstrip("/")


def _site(path: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}{path}"


def _landing_url(slug: str) -> str:
    # Канонический адрес статьи — всегда /uz/..., как в sitemap и <link rel=
    # canonical> фронта. Отдавать локаль харвестера значило бы наплодить в
    # чужих базах пять дублей одной статьи.
    return _site(f"/uz/article/{slug}")


def _pdf_url(slug: str) -> str:
    return _site(f"/pdf/{slug}.pdf")


def _sub(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = text
    return el


def _envelope(request: Request, verb: str | None, args: dict[str, str]) -> ET.Element:
    root = ET.Element(f"{{{OAI_NS}}}OAI-PMH")
    root.set(
        f"{{{XSI_NS}}}schemaLocation",
        f"{OAI_NS} http://www.openarchives.org/OAI/2.0/OAI-PMH.xsd",
    )
    _sub(root, f"{{{OAI_NS}}}responseDate", format_datestamp(datetime.now(timezone.utc)))
    req = _sub(root, f"{{{OAI_NS}}}request", _base_url(request))
    if verb:
        req.set("verb", verb)
    for key, value in args.items():
        req.set(key, value)
    return root


def _xml(root: ET.Element) -> Response:
    body = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="utf-8", xml_declaration=False
    )
    # Выдача адресная и закрытая — незачем оставлять её в промежуточных кэшах.
    return Response(
        content=body,
        media_type="application/xml; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"},
    )


def _error(request: Request, verb: str | None, args: dict, code: str, msg: str) -> Response:
    # В <request> при ошибке спецификация велит не повторять аргументы —
    # оставляем только глагол.
    root = _envelope(request, verb if code != "badVerb" else None, {})
    el = _sub(root, f"{{{OAI_NS}}}error", msg)
    el.set("code", code)
    return _xml(root)


def _echo(params: dict[str, str]) -> dict[str, str]:
    """Аргументы для эха в <request> — без ключа доступа."""
    return {k: v for k, v in params.items() if k not in ("verb", TOKEN_QUERY_PARAM)}


# --------------------------------------------------------------- запись → DC

def _dc_record(row, authors: list[str], scope: Scope) -> ET.Element:
    article, issue, journal, publisher = row

    meta = ET.Element(f"{{{OAI_DC_NS}}}dc")
    meta.set(
        f"{{{XSI_NS}}}schemaLocation",
        f"{OAI_DC_NS} {_METADATA_FORMATS['oai_dc'][1]}",
    )

    def dc(tag: str, value) -> None:
        if value is None:
            return
        text = str(value).strip()
        if text:
            _sub(meta, f"{{{DC_NS}}}{tag}", text)

    dc("title", article.title)
    dc("title", article.title_foreign)

    # Структурированные авторы точнее свободного поля: там встречаются и
    # «Иванов И.И., Петров П.П.», и «Иванов И.И.; Петров П.П.».
    if authors:
        for name in authors:
            dc("creator", name)
    elif article.authors:
        for name in _SPLIT_RE.split(article.authors):
            dc("creator", name)

    for source in (article.keywords, article.keywords_foreign):
        if source:
            for word in _SPLIT_RE.split(source):
                dc("subject", word)
    dc("subject", article.field_of_science)

    dc("description", article.annotation)
    dc("description", article.annotation_foreign)

    # Издатель: платформенный издатель → издатель журнала → свободное поле.
    dc(
        "publisher",
        (publisher.name if publisher else None)
        or (journal.publisher if journal else None)
        or article.publisher,
    )

    if article.data:
        dc("date", article.data.isoformat())
    elif article.publication_year:
        dc("date", article.publication_year)

    dc("type", article.publication_type)
    dc("type", "Text")

    dc("identifier", _landing_url(article.slug))
    if article.doi:
        doi = article.doi.strip()
        dc("identifier", doi if doi.startswith("http") else f"https://doi.org/{doi}")
    if article.isbn:
        dc("identifier", f"urn:isbn:{article.isbn.strip()}")

    # Полный текст — только тем ключам, кому он открыт (include_fulltext).
    if scope.include_fulltext and article.pdf:
        dc("identifier", _pdf_url(article.slug))
        dc("format", "application/pdf")

    # Библиографическая привязка: том/номер/страницы одной строкой — в oai_dc
    # отдельных полей для них нет.
    if journal is not None:
        bits = [journal.name]
        if issue is not None:
            if issue.volume:
                bits.append(f"Vol. {issue.volume}")
            if issue.issue:
                bits.append(f"No. {issue.issue}")
            if issue.year:
                bits.append(str(issue.year))
        if article.pages:
            bits.append(f"pp. {article.pages}")
        dc("source", ", ".join(b for b in bits if b))
        dc("source", journal.issn)
        dc("source", journal.printed_issn)
    if issue is not None and issue.isbn:
        dc("source", f"ISBN {issue.isbn}")

    # Лицензии в базе пока нет — выдумывать права нельзя. Как только у журнала
    # появится metadata.license, она поедет сюда (и её сразу спросит EBSCO).
    if journal is not None and isinstance(journal.meta, dict):
        dc("rights", journal.meta.get("license"))

    return meta


def _identifier(article_id: int) -> str:
    return f"oai:{settings.OAI_NAMESPACE}:article/{article_id}"


def _header(row) -> ET.Element:
    article, issue, journal, _publisher = row
    header = ET.Element(f"{{{OAI_NS}}}header")
    _sub(header, f"{{{OAI_NS}}}identifier", _identifier(article.id))
    _sub(header, f"{{{OAI_NS}}}datestamp", format_datestamp(article.updated_at))
    if journal is not None and journal.slug:
        _sub(header, f"{{{OAI_NS}}}setSpec", f"journal:{journal.slug}")
    if article.publication_type:
        _sub(header, f"{{{OAI_NS}}}setSpec", f"type:{article.publication_type}")
    return header


# --------------------------------------------------------------- аргументы

_ALLOWED_ARGS = {
    "Identify": set(),
    "ListMetadataFormats": {"identifier"},
    "ListSets": {"resumptionToken"},
    "ListIdentifiers": {"from", "until", "set", "metadataPrefix", "resumptionToken"},
    "ListRecords": {"from", "until", "set", "metadataPrefix", "resumptionToken"},
    "GetRecord": {"identifier", "metadataPrefix"},
}


def _check_args(verb: str, params: dict[str, str]) -> None:
    extra = set(params) - _ALLOWED_ARGS[verb] - {"verb", TOKEN_QUERY_PARAM}
    if extra:
        raise OaiError("badArgument", f"Unexpected argument(s): {', '.join(sorted(extra))}")


def _check_prefix(prefix: str | None) -> str:
    if not prefix:
        raise OaiError("badArgument", "metadataPrefix is required")
    if prefix not in _METADATA_FORMATS:
        raise OaiError("cannotDisseminateFormat", f"Unsupported metadataPrefix: {prefix}")
    return prefix


def _selector_from_params(params: dict[str, str]) -> Selector:
    frm = params.get("from")
    until = params.get("until")
    selector = Selector(
        frm=parse_datestamp(frm, end_of_day=False) if frm else None,
        until=parse_datestamp(until, end_of_day=True) if until else None,
        set_spec=params.get("set") or None,
    )
    if selector.frm and selector.until and selector.frm > selector.until:
        raise OaiError("badArgument", "from is later than until")
    # Верхнюю границу фиксируем сразу: обход большой базы идёт часами, и без
    # неё записи, изменённые по ходу харвеста, съезжали бы между страницами.
    if selector.until is None:
        selector.until = datetime.now(timezone.utc)
    return selector


# --------------------------------------------------------------- глаголы

async def _identify(request: Request, db: AsyncSession, scope: Scope) -> ET.Element:
    root = _envelope(request, "Identify", {})
    body = _sub(root, f"{{{OAI_NS}}}Identify")
    _sub(body, f"{{{OAI_NS}}}repositoryName", settings.OAI_REPOSITORY_NAME)
    _sub(body, f"{{{OAI_NS}}}baseURL", _base_url(request))
    _sub(body, f"{{{OAI_NS}}}protocolVersion", "2.0")
    _sub(body, f"{{{OAI_NS}}}adminEmail", settings.OAI_ADMIN_EMAIL)
    _sub(
        body,
        f"{{{OAI_NS}}}earliestDatestamp",
        format_datestamp(await domain.earliest(db, scope)),
    )
    # Снятую с публикации статью мы не помечаем удалённой, а просто перестаём
    # отдавать — это и означает deletedRecord = no.
    _sub(body, f"{{{OAI_NS}}}deletedRecord", "no")
    _sub(body, f"{{{OAI_NS}}}granularity", GRANULARITY)

    desc = _sub(body, f"{{{OAI_NS}}}description")
    ident = ET.SubElement(desc, f"{{{ID_NS}}}oai-identifier")
    ident.set(f"{{{XSI_NS}}}schemaLocation", f"{ID_NS} {ID_NS}.xsd")
    _sub(ident, f"{{{ID_NS}}}scheme", "oai")
    _sub(ident, f"{{{ID_NS}}}repositoryIdentifier", settings.OAI_NAMESPACE)
    _sub(ident, f"{{{ID_NS}}}delimiter", ":")
    _sub(ident, f"{{{ID_NS}}}sampleIdentifier", _identifier(1))
    return root


async def _list_metadata_formats(request: Request, params: dict) -> ET.Element:
    root = _envelope(request, "ListMetadataFormats", _echo(params))
    body = _sub(root, f"{{{OAI_NS}}}ListMetadataFormats")
    for prefix, (namespace, schema) in _METADATA_FORMATS.items():
        fmt = _sub(body, f"{{{OAI_NS}}}metadataFormat")
        _sub(fmt, f"{{{OAI_NS}}}metadataPrefix", prefix)
        _sub(fmt, f"{{{OAI_NS}}}schema", schema)
        _sub(fmt, f"{{{OAI_NS}}}metadataNamespace", namespace)
    return root


async def _list_sets(
    request: Request, db: AsyncSession, scope: Scope, params: dict
) -> ET.Element:
    sets = await domain.sets(db, scope)
    if not sets:
        raise OaiError("noSetHierarchy", "No sets available for this key")
    root = _envelope(request, "ListSets", _echo(params))
    body = _sub(root, f"{{{OAI_NS}}}ListSets")
    for spec, name in sets:
        el = _sub(body, f"{{{OAI_NS}}}set")
        _sub(el, f"{{{OAI_NS}}}setSpec", spec)
        _sub(el, f"{{{OAI_NS}}}setName", name)
    return root


async def _list(
    request: Request,
    db: AsyncSession,
    scope: Scope,
    params: dict,
    verb: str,
) -> ET.Element:
    token = params.get("resumptionToken")
    if token:
        if set(params) - {"verb", "resumptionToken", TOKEN_QUERY_PARAM}:
            raise OaiError(
                "badArgument", "resumptionToken must be the only argument"
            )
        selector, cursor, total, prefix = decode_token(token)
    else:
        prefix = _check_prefix(params.get("metadataPrefix"))
        selector = _selector_from_params(params)
        cursor, total = None, None

    limit = max(1, settings.OAI_PAGE_SIZE)
    # +1 запись сверх страницы — дешёвая проверка «есть ли что дальше», без
    # второго COUNT на каждой странице.
    rows = await domain.page(db, scope, selector, cursor, limit + 1)
    has_more = len(rows) > limit
    rows = rows[:limit]

    if not rows and cursor is None:
        raise OaiError("noRecordsMatch", "No records match the request")
    if total is None:
        total = await domain.count(db, scope, selector)

    root = _envelope(request, verb, _echo(params))
    body = _sub(root, f"{{{OAI_NS}}}{verb}")

    authors = (
        await domain.authors(db, [r[0].id for r in rows])
        if verb == "ListRecords"
        else {}
    )
    for row in rows:
        if verb == "ListIdentifiers":
            body.append(_header(row))
        else:
            record = _sub(body, f"{{{OAI_NS}}}record")
            record.append(_header(row))
            metadata = _sub(record, f"{{{OAI_NS}}}metadata")
            metadata.append(_dc_record(row, authors.get(row[0].id, []), scope))

    # resumptionToken отдаём всегда, когда список продолжается; пустой элемент
    # в конце — сигнал «обход завершён», его ждут аккуратные харвестеры.
    rt = _sub(body, f"{{{OAI_NS}}}resumptionToken")
    rt.set("completeListSize", str(total))
    if has_more and rows:
        last = rows[-1][0]
        rt.text = encode_token(selector, (last.updated_at, last.id), total, prefix)
    return root


async def _get_record(
    request: Request, db: AsyncSession, scope: Scope, params: dict
) -> ET.Element:
    _check_prefix(params.get("metadataPrefix"))
    identifier = params.get("identifier")
    if not identifier:
        raise OaiError("badArgument", "identifier is required")
    match = _IDENTIFIER_RE.match(identifier.strip())
    if not match or match.group("ns") != settings.OAI_NAMESPACE:
        raise OaiError("idDoesNotExist", f"Unknown identifier: {identifier}")

    row = await domain.get_one(db, scope, int(match.group("id")))
    if row is None:
        # Неопубликованная, демо- или чужая (вне allowed_journal_ids) запись
        # неотличима от несуществующей — так и должно быть.
        raise OaiError("idDoesNotExist", f"Unknown identifier: {identifier}")

    authors = await domain.authors(db, [row[0].id])
    root = _envelope(request, "GetRecord", _echo(params))
    body = _sub(root, f"{{{OAI_NS}}}GetRecord")
    record = _sub(body, f"{{{OAI_NS}}}record")
    record.append(_header(row))
    metadata = _sub(record, f"{{{OAI_NS}}}metadata")
    metadata.append(_dc_record(row, authors.get(row[0].id, []), scope))
    return root


# --------------------------------------------------------------- эндпоинт

@router.api_route("", methods=["GET", "POST"], include_in_schema=False)
@router.api_route("/", methods=["GET", "POST"], include_in_schema=False)
@limiter.limit(settings.OAI_RATE_LIMIT)
async def oai_endpoint(request: Request, db: AsyncSession = Depends(get_db)):
    # Ключ проверяем ДО разбора протокола: посторонний не должен по разнице
    # ответов выяснять даже то, какие глаголы мы поддерживаем.
    client = await authenticate(request, db)
    scope = Scope.from_client(client)

    params = dict(request.query_params)
    if request.method == "POST":
        form = await request.form()
        params.update({k: str(v) for k, v in form.items()})

    verb = params.get("verb")
    try:
        if verb not in _ALLOWED_ARGS:
            raise OaiError("badVerb", f"Illegal or missing verb: {verb or '(none)'}")
        _check_args(verb, params)

        if verb == "Identify":
            root = await _identify(request, db, scope)
        elif verb == "ListMetadataFormats":
            root = await _list_metadata_formats(request, params)
        elif verb == "ListSets":
            root = await _list_sets(request, db, scope, params)
        elif verb == "GetRecord":
            root = await _get_record(request, db, scope, params)
        else:
            root = await _list(request, db, scope, params, verb)
    except OaiError as exc:
        return _error(request, verb, params, exc.code, exc.message)

    return _xml(root)

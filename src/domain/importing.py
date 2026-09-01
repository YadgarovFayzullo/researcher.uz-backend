"""Импорт архивов с других платформ — разбор источника, проверка, применение.

Замысел и рамки — в `import-integration.md` (репозиторий фронта). Коротко:
клиент заливает таблицу со старыми статьями и PDF к ним, строки оседают в
`import_items`, он смотрит превью и правит спорное, и только потом задача
применяется — создаёт выпуски и статьи ЧЕРНОВИКАМИ (`published = false`).

Три причины, почему промежуточная таблица, а не запись сразу:
1. Чужие данные грязные — писать их прямо в витрину нельзя.
2. Импорт длинный и рвётся посередине — нужно состояние, чтобы продолжить.
3. Клиенту нужен отчёт: что создано, что дубликат, что упало и почему.

Этот модуль ничего не знает про HTTP: ошибки — доменные исключения, транспорт
их переводит в статусы (см. `src/api/v1/imports.py`).
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import time
import unicodedata
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:  # только для аннотаций — модуль тянет httpx, не нужный CSV-пути
    from src.infrastructure.external.landing import LandingData
    from src.infrastructure.external.oai import OaiRecord

from slugify import slugify
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.article import ArticleDomain
from src.infrastructure.persistence.models import (
    Article,
    ImportItem,
    ImportJob,
    Issue,
)

# Потолок на задачу: один клиент не должен занять обработчик на часы.
MAX_ITEMS_PER_JOB = 2000
# Размер пачки при применении. Транзакция на пачку, а не на всю задачу: 300
# статей одной транзакцией держат блокировки минутами и рвутся целиком.
APPLY_CHUNK = 25
# Задача в 'applying' без отметки живости дольше этого — оборванный прогон.
STALE_AFTER_SECONDS = 120

# Колонки шаблона таблицы. Ключ — наше поле, значения — принимаемые заголовки
# (клиенты переименовывают колонки и пишут их на трёх языках).
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("title", "название", "заголовок", "sarlavha", "maqola nomi"),
    "title_foreign": ("title_en", "title_foreign", "название_en", "название на английском"),
    "authors": ("authors", "author", "авторы", "автор", "mualliflar"),
    "annotation": ("annotation", "abstract", "аннотация", "annotatsiya"),
    "annotation_foreign": ("annotation_en", "abstract_en", "аннотация_en"),
    "keywords": ("keywords", "ключевые слова", "kalit so'zlar"),
    "keywords_foreign": ("keywords_en", "ключевые слова_en"),
    "pages": ("pages", "страницы", "betlar"),
    "doi": ("doi",),
    "year": ("year", "год", "yil"),
    "volume": ("volume", "том", "tom"),
    "issue": ("issue", "номер", "выпуск", "son"),
    "field_of_science": ("field_of_science", "область науки", "направление"),
    "pdf_filename": ("pdf_filename", "pdf", "файл", "имя файла", "fayl"),
}

TEMPLATE_COLUMNS = tuple(COLUMN_ALIASES.keys())

# Порядок важен: сначала пробуем ISO, потом «человеческие» форматы.
_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d")


class ImportError_(Exception):
    """Ошибка импорта, о которой должен узнать клиент."""


class JobNotFound(ImportError_):
    pass


class JobBusy(ImportError_):
    """Над задачей уже идёт работа — второй прогон запускать нельзя."""


# ---------------------------------------------------------------------------
# Нормализация значений
# ---------------------------------------------------------------------------

def normalize_doi(doi: str | None) -> str | None:
    """`https://doi.org/10.X/Y`, `doi:10.X/Y`, `10.X/Y` → `10.x/y`."""
    if not doi:
        return None
    s = str(doi).strip().lower()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s)
    s = re.sub(r"^doi:\s*", "", s)
    s = s.strip()
    return s if s.startswith("10.") else None


def normalize_title(title: str | None) -> str:
    """Ключ для поиска дубликатов: без регистра, пунктуации и лишних пробелов."""
    if not title:
        return ""
    s = unicodedata.normalize("NFKD", str(title)).lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def parse_year(value: Any) -> int | None:
    """Год из «2019», «2019 г.», «01.05.2019» — что угодно с четырьмя цифрами."""
    if value in (None, ""):
        return None
    m = re.search(r"(19|20)\d{2}", str(value))
    return int(m.group(0)) if m else None


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def split_authors(value: Any) -> str | None:
    """Авторы приходят одной ячейкой.

    Разделитель — `;`, а запятая НЕ разделитель: «Иванов, И.И.» — это один
    автор, и по запятой он развалился бы на двух. Если точки с запятой нет,
    строку оставляем как есть — угадывать дороже, чем показать клиенту в
    превью то, что он написал.
    """
    if value in (None, ""):
        return None
    parts = [p.strip() for p in str(value).split(";") if p.strip()]
    return ", ".join(parts) if parts else str(value).strip() or None


def issue_key_of(year: int | None, volume: Any, issue: Any) -> str:
    """`2024|1|3` — по этому ключу строки группируются в выпуски."""
    def _s(v: Any) -> str:
        return "" if v in (None, "") else str(v).strip()

    return f"{year or ''}|{_s(volume)}|{_s(issue)}"


# ---------------------------------------------------------------------------
# Разбор таблицы
# ---------------------------------------------------------------------------

def _match_column(header: str) -> str | None:
    h = (header or "").strip().lower().lstrip("﻿")
    for field, aliases in COLUMN_ALIASES.items():
        if h == field or h in aliases:
            return field
    return None


def _rows_from_csv(content: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    # utf-8-sig съедает BOM, который Excel добавляет при «Сохранить как CSV».
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ImportError_("Не удалось прочитать файл: неизвестная кодировка")

    # Разделитель бывает и `,`, и `;` (Excel в русской локали) — определяем сами.
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = list(reader.fieldnames or [])
    return headers, [dict(row) for row in reader]


def _rows_from_xlsx(content: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    try:
        from openpyxl import load_workbook
    except ImportError:  # pragma: no cover — зависимость есть в requirements
        raise ImportError_("Чтение XLSX недоступно: не установлен openpyxl")

    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        headers = [str(h).strip() if h is not None else "" for h in next(rows)]
    except StopIteration:
        return [], []
    out: list[dict[str, Any]] = []
    for values in rows:
        if values is None or all(v in (None, "") for v in values):
            continue
        out.append({headers[i]: v for i, v in enumerate(values) if i < len(headers)})
    wb.close()
    return headers, out


def parse_table(content: bytes, filename: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Файл → список строк в терминах наших полей + список неузнанных колонок."""
    if filename.lower().endswith((".xlsx", ".xlsm")):
        headers, raw_rows = _rows_from_xlsx(content)
    else:
        headers, raw_rows = _rows_from_csv(content)

    mapping = {h: _match_column(h) for h in headers}
    unknown = [h for h, field in mapping.items() if field is None and (h or "").strip()]
    if not any(field == "title" for field in mapping.values()):
        raise ImportError_(
            "В таблице нет колонки с названием статьи. Скачайте шаблон и "
            "сохраните данные в его колонки."
        )

    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        row: dict[str, Any] = {}
        for header, value in raw.items():
            field = mapping.get(header)
            if field and value not in (None, ""):
                row[field] = value if isinstance(value, (int, float)) else str(value).strip()
        if row:
            rows.append({"_raw": {k: str(v) for k, v in raw.items() if v not in (None, "")}, **row})
    return rows, unknown


def build_parsed(row: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Строка таблицы → поля статьи + список замечаний к ней."""
    problems: list[dict[str, str]] = []
    title = (row.get("title") or "").strip()
    if not title:
        problems.append({"field": "title", "code": "required", "message": "Пустое название"})

    year = parse_year(row.get("year"))
    if row.get("year") and year is None:
        problems.append({"field": "year", "code": "bad_year", "message": "Не удалось разобрать год"})
    if year is not None and not (1900 <= year <= date.today().year):
        problems.append(
            {"field": "year", "code": "out_of_range", "message": f"Год вне диапазона: {year}"}
        )
        year = None

    doi_raw = row.get("doi")
    doi = normalize_doi(doi_raw)
    if doi_raw and not doi:
        problems.append({"field": "doi", "code": "bad_doi", "message": f"Непохоже на DOI: {doi_raw}"})

    # Дата публикации: articles.data ограничена CHECK (data <= CURRENT_DATE),
    # поэтому будущие даты и «31 декабря» текущего года не подставляем — берём
    # 1 января года выпуска, а для текущего года оставляем пусто (сервер
    # подставит CURRENT_DATE).
    pub_date: date | None = None
    if year is not None and year < date.today().year:
        pub_date = date(year, 1, 1)

    parsed = {
        "title": title,
        "title_foreign": row.get("title_foreign") or None,
        "authors": split_authors(row.get("authors")),
        "annotation": row.get("annotation") or None,
        "annotation_foreign": row.get("annotation_foreign") or None,
        "keywords": row.get("keywords") or None,
        "keywords_foreign": row.get("keywords_foreign") or None,
        "pages": str(row.get("pages")).strip() if row.get("pages") not in (None, "") else None,
        "doi": doi,
        "publication_year": year,
        "data": pub_date.isoformat() if pub_date else None,
        "field_of_science": row.get("field_of_science") or None,
        "volume": str(row["volume"]).strip() if row.get("volume") not in (None, "") else None,
        "issue": str(row["issue"]).strip() if row.get("issue") not in (None, "") else None,
    }
    return parsed, problems


def _pick_languages(values: dict[str, str], preferred: str | None) -> tuple[str | None, str | None]:
    """Многоязычное поле OAI → пара «основной / иностранный».

    Основным берём язык самой статьи (`dc:language`), а если его нет — первый
    попавшийся. Иностранным — английский, если он не основной; иначе любой
    оставшийся. Так узбекская статья с английским переводом ложится в наши
    `title` / `title_foreign` тем же способом, что и при ручном вводе.
    """
    if not values:
        return None, None

    def by_prefix(prefix: str) -> str | None:
        for lang, value in values.items():
            if lang.split("-")[0] == prefix:
                return value
        return None

    main_lang = (preferred or "").lower()[:2]
    main = by_prefix(main_lang) if main_lang else None
    if main is None:
        main = next(iter(values.values()))

    foreign = None
    for candidate in ("en", "ru"):
        value = by_prefix(candidate)
        if value and value != main:
            foreign = value
            break
    if foreign is None:
        foreign = next((v for v in values.values() if v != main), None)
    return main, foreign


def parsed_from_oai(record: "OaiRecord") -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Запись OAI → поля статьи (без обращения к landing page)."""
    from src.infrastructure.external.oai import parse_source

    source = parse_source(record.source)
    lang = (record.language or "").lower()
    # `dc:language` бывает трёхбуквенным (`rus`, `uzb`) — приводим к двум.
    lang = {"rus": "ru", "eng": "en", "uzb": "uz"}.get(lang, lang)

    title, title_foreign = _pick_languages(record.titles, lang)
    annotation, annotation_foreign = _pick_languages(record.descriptions, lang)

    keywords_by_lang = {k: ", ".join(v) for k, v in record.subjects.items() if v}
    keywords, keywords_foreign = _pick_languages(keywords_by_lang, lang)

    # Авторы приходят по списку на каждый язык — берём вариант основного языка,
    # иначе первый список; склеивать все варианты нельзя, это одни и те же люди.
    authors_list: list[str] = []
    if record.creators:
        by_lang = {k.split("-")[0]: v for k, v in record.creators.items()}
        authors_list = by_lang.get(lang) or next(iter(record.creators.values()))

    year = source.get("year") or parse_year(record.date)
    row = {
        "title": title or "",
        "title_foreign": title_foreign,
        "authors": ", ".join(authors_list) if authors_list else None,
        "annotation": annotation,
        "annotation_foreign": annotation_foreign,
        "keywords": keywords,
        "keywords_foreign": keywords_foreign,
        "pages": source.get("pages"),
        "doi": record.doi,
        "year": year,
        "volume": source.get("volume"),
        "issue": source.get("issue"),
    }
    parsed, problems = build_parsed(row)
    # Многоязычные поля build_parsed не знает — доносим их поверх.
    parsed["title_foreign"] = title_foreign
    parsed["annotation_foreign"] = annotation_foreign
    parsed["keywords_foreign"] = keywords_foreign
    parsed["landing_url"] = record.landing_url
    if record.rights:
        parsed["rights"] = record.rights[0]
    return parsed, problems


def merge_landing(parsed: dict[str, Any], landing: "LandingData") -> dict[str, Any]:
    """Дополнить поля статьи тем, что нашлось на её странице у источника.

    Заполняем ТОЛЬКО пустое: то, что клиент уже поправил руками в превью,
    чужой сайт перебивать не должен.

    Год здесь важнее остального: в `oai_dc` он есть не всегда (КиберЛенинка не
    отдаёт ни даты, ни выпуска — только название, автора и ссылку), а без года
    все статьи архива легли бы в один выпуск.
    """
    out = dict(parsed)
    if not out.get("doi") and landing.doi:
        out["doi"] = normalize_doi(landing.doi)
    if not out.get("pages") and landing.pages:
        out["pages"] = landing.pages
    if not out.get("authors") and landing.authors:
        out["authors"] = ", ".join(landing.authors)
    if not out.get("volume") and landing.volume:
        out["volume"] = landing.volume
    if not out.get("issue") and landing.issue:
        out["issue"] = landing.issue
    if not out.get("annotation") and landing.abstract:
        out["annotation"] = landing.abstract
    if not out.get("keywords") and landing.keywords:
        out["keywords"] = ", ".join(landing.keywords)
    if not out.get("publication_year"):
        year = parse_year(landing.date)
        if year is not None and 1900 <= year <= date.today().year:
            out["publication_year"] = year
            # Та же осторожность, что в build_parsed: articles.data ограничена
            # CHECK (data <= CURRENT_DATE), поэтому для текущего года дату не
            # выдумываем.
            if year < date.today().year:
                out["data"] = date(year, 1, 1).isoformat()
    return out


def source_key_of(row: dict[str, Any], parsed: dict[str, Any]) -> str:
    """Ключ строки в источнике: DOI, если есть, иначе хеш названия и выпуска.

    Нужен, чтобы повторная заливка того же файла не двоила строки внутри задачи
    (UNIQUE (job_id, source_key)).
    """
    if parsed.get("doi"):
        return f"doi:{parsed['doi']}"
    base = "|".join(
        [
            normalize_title(parsed.get("title")),
            str(parsed.get("publication_year") or ""),
            str(parsed.get("volume") or ""),
            str(parsed.get("issue") or ""),
            str(row.get("pdf_filename") or ""),
        ]
    )
    return "row:" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:20]


class ImportDomain:
    """Операции над задачей импорта. Права проверяет транспортный слой."""

    def __init__(self) -> None:
        self.articles = ArticleDomain()

    # ------------------------------------------------------------- задачи
    async def get_job(self, db: AsyncSession, job_id: int) -> ImportJob:
        job = (
            await db.execute(select(ImportJob).where(ImportJob.id == job_id))
        ).scalars().first()
        if job is None:
            raise JobNotFound("Задача импорта не найдена")
        return job

    async def create_job(
        self,
        db: AsyncSession,
        *,
        journal_id: int,
        created_by,
        source_type: str = "table",
        source_ref: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> ImportJob:
        job = ImportJob(
            journal_id=journal_id,
            created_by=created_by,
            source_type=source_type,
            source_ref=source_ref,
            params=params or {},
            status="draft",
            totals={},
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return job

    async def list_jobs(self, db: AsyncSession, journal_id: int) -> list[ImportJob]:
        return list(
            (
                await db.execute(
                    select(ImportJob)
                    .where(ImportJob.journal_id == journal_id)
                    .order_by(ImportJob.created_at.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )

    # -------------------------------------------------------------- разбор
    async def load_table(
        self, db: AsyncSession, job: ImportJob, content: bytes, filename: str
    ) -> dict[str, Any]:
        """Разобрать таблицу в import_items и сразу проверить кандидатов."""
        if job.status in ("applying", "parsing"):
            raise JobBusy("Задача уже обрабатывается")

        rows, unknown_columns = parse_table(content, filename)
        if not rows:
            raise ImportError_("В файле нет строк с данными")
        if len(rows) > MAX_ITEMS_PER_JOB:
            raise ImportError_(
                f"Слишком много строк: {len(rows)}. Максимум за раз — {MAX_ITEMS_PER_JOB}, "
                "разбейте архив на части."
            )

        # Повторная заливка файла в ту же задачу заменяет прежние кандидаты:
        # уже созданные статьи трогать нельзя, поэтому чистим только не
        # применённое.
        await db.execute(
            ImportItem.__table__.delete().where(
                ImportItem.job_id == job.id, ImportItem.status != "created"
            )
        )

        seen: set[str] = set()
        items: list[ImportItem] = []
        for row in rows:
            parsed, problems = build_parsed(row)
            key = source_key_of(row, parsed)
            if key in seen:
                # Дубль внутри самого файла: не заводим второй раз, но и не
                # молчим — счётчик уйдёт в отчёт.
                continue
            seen.add(key)
            items.append(
                ImportItem(
                    job_id=job.id,
                    source_key=key,
                    raw=row.get("_raw", {}),
                    parsed=parsed,
                    issue_key=issue_key_of(
                        parsed.get("publication_year"), parsed.get("volume"), parsed.get("issue")
                    ),
                    status="invalid" if problems else "pending",
                    problems=problems,
                    pdf_source=(str(row["pdf_filename"]).strip() if row.get("pdf_filename") else None),
                )
            )

        db.add_all(items)
        job.source_ref = filename
        job.status = "parsing"
        await db.commit()

        await self.revalidate(db, job)
        return {"parsed": len(items), "skipped_in_file": len(rows) - len(items), "unknown_columns": unknown_columns}

    async def load_oai(
        self, db: AsyncSession, job: ImportJob, *, resume: bool = False
    ) -> dict[str, Any]:
        """Забрать записи со старого сайта и разложить их в кандидаты.

        Метаданные берём из OAI одним обходом. Landing page здесь НЕ трогаем:
        это запрос на каждую статью, и для архива в тысячу работ разбор занял бы
        полчаса. Добогащение и файлы — на этапе применения, только для тех
        записей, которые клиент действительно импортирует.
        """
        from src.infrastructure.external.oai import OaiError, list_records

        params = job.params or {}
        base_url = params.get("base_url")
        if not base_url:
            raise ImportError_("В задаче не указан адрес репозитория")

        # Продолжение обхода: архив бывает больше потолка задачи, и тогда
        # клиент дозабирает хвост той же задачей, а не начинает заново.
        resume_token = params.get("resume_token") if resume else None

        try:
            page = await list_records(
                base_url,
                set_spec=params.get("set"),
                date_from=params.get("from"),
                date_until=params.get("until"),
                limit=MAX_ITEMS_PER_JOB,
                resume_token=resume_token,
            )
        except OaiError as e:
            raise ImportError_(str(e)) from e

        records = page.records
        if not records and not resume:
            raise ImportError_(
                "Репозиторий не отдал ни одной записи. Проверьте выбранный журнал "
                "и диапазон дат."
            )

        if not resume:
            await db.execute(
                ImportItem.__table__.delete().where(
                    ImportItem.job_id == job.id, ImportItem.status != "created"
                )
            )
            existing_keys: set[str] = set()
        else:
            # При догрузке прежние кандидаты остаются; ключи нужны, чтобы не
            # налететь на UNIQUE (job_id, source_key) при пересечении страниц.
            existing_keys = set(
                (
                    await db.execute(
                        select(ImportItem.source_key).where(ImportItem.job_id == job.id)
                    )
                )
                .scalars()
                .all()
            )

        items: list[ImportItem] = []
        seen: set[str] = set(existing_keys)
        for record in records:
            parsed, problems = parsed_from_oai(record)
            key = record.identifier or source_key_of({}, parsed)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                ImportItem(
                    job_id=job.id,
                    source_key=key,
                    raw={
                        "identifier": record.identifier,
                        "source": record.source,
                        "landing_url": record.landing_url,
                        "datestamp": record.datestamp,
                    },
                    parsed=parsed,
                    issue_key=issue_key_of(
                        parsed.get("publication_year"), parsed.get("volume"), parsed.get("issue")
                    ),
                    status="invalid" if problems else "pending",
                    problems=problems,
                    # Файл известен только после захода на landing page —
                    # помечаем намерение, а не готовую ссылку.
                    pdf_source=record.landing_url,
                )
            )

        # Глубокий разбор: заходим на страницу каждой статьи уже сейчас.
        # Нужен источникам, у которых в `oai_dc` нет ни дат, ни выпусков
        # (КиберЛенинка отдаёт только название, автора и ссылку): без года все
        # статьи легли бы в один выпуск, а отобрать нужный год в превью клиент
        # бы не смог. По умолчанию включаем сами, когда дат нет ни у одной
        # записи; `deep` в параметрах задачи переопределяет решение.
        # Шаг дорогой — запрос на статью с паузой между обращениями к хосту,
        # поэтому обычному OJS он не достаётся.
        deep = params.get("deep")
        if deep is None:
            deep = not any(record.date for record in records)
        want_year = parse_year(params.get("year"))
        want_journal = normalize_title(params.get("journal_title") or "") or None
        landing_failed = 0
        skipped_by_year = 0
        skipped_by_journal = 0
        if deep:
            kept: list[ImportItem] = []
            for item in items:
                if not await self._fill_item_from_landing(item):
                    landing_failed += 1
                parsed_item = item.parsed or {}
                # Отбор по году: год известен только со страницы статьи,
                # поэтому фильтруем здесь, а не запросом к репозиторию.
                if want_year is not None and parsed_item.get("publication_year") != want_year:
                    skipped_by_year += 1
                    continue
                if want_journal:
                    source_journal = normalize_title(parsed_item.get("source_journal") or "")
                    if source_journal and source_journal != want_journal:
                        skipped_by_journal += 1
                        continue
                kept.append(item)
            items = kept

        db.add_all(items)
        # Токен продолжения храним в задаче: он и есть закладка в чужом архиве.
        job.params = {
            **params,
            "resume_token": page.resume_token,
            "total_in_repository": page.total_in_repository,
            # Итоги последнего обхода: без них клиент не поймёт, куда делись
            # записи, отброшенные фильтром года.
            "scan": {
                "records": len(records),
                "kept": len(items),
                "skipped_by_year": skipped_by_year,
                "skipped_by_journal": skipped_by_journal,
                "landing_failed": landing_failed,
                "deep": bool(deep),
            },
        }
        job.status = "parsing"
        await db.commit()
        await self.revalidate(db, job)
        return {
            "parsed": len(items),
            "skipped_in_file": len(records) - len(items),
            "unknown_columns": [],
        }

    async def _fill_item_from_landing(self, item: ImportItem) -> bool:
        """Дозаполнить кандидата со страницы статьи. False — страница не далась.

        PDF здесь не трогаем: файл качается при применении, только для статей,
        которые клиент действительно импортирует.
        """
        from src.infrastructure.external.landing import fetch_landing
        from src.infrastructure.external.safe_fetch import FetchError

        parsed = dict(item.parsed or {})
        landing_url = parsed.get("landing_url") or item.pdf_source
        if not landing_url:
            return False
        try:
            landing = await fetch_landing(landing_url)
        except FetchError as e:
            item.problems = [
                *(item.problems or []),
                {"field": "", "code": "landing_failed", "message": f"Страница статьи недоступна: {e}"},
            ]
            return False

        item.parsed = merge_landing(parsed, landing)
        # Название журнала у источника: набор OAI бывает шире самого журнала
        # (в наборе КиберЛенинки записей больше, чем статей на карточке
        # журнала), и без этой проверки в журнал приехало бы чужое.
        if landing.journal_title:
            item.parsed["source_journal"] = landing.journal_title
        item.issue_key = issue_key_of(
            item.parsed.get("publication_year"),
            item.parsed.get("volume"),
            item.parsed.get("issue"),
        )
        return True

    async def revalidate(self, db: AsyncSession, job: ImportJob) -> None:
        """Пересчитать дубликаты и сводку. Дёшево — идёт после каждой правки."""
        items = list(
            (
                await db.execute(
                    select(ImportItem).where(
                        ImportItem.job_id == job.id, ImportItem.status != "created"
                    )
                )
            )
            .scalars()
            .all()
        )

        existing_dois, existing_titles = await self._existing_keys(db, job.journal_id)

        for item in items:
            parsed = item.parsed or {}
            problems = [p for p in (item.problems or []) if p.get("code") != "duplicate"]
            status = "invalid" if problems else "pending"

            doi = parsed.get("doi")
            title_key = normalize_title(parsed.get("title"))
            year = parsed.get("publication_year")
            if doi and doi in existing_dois:
                status = "duplicate"
                problems.append(
                    {"field": "doi", "code": "duplicate", "message": "Статья с таким DOI уже есть"}
                )
            elif title_key and (title_key, year) in existing_titles:
                status = "duplicate"
                problems.append(
                    {
                        "field": "title",
                        "code": "duplicate",
                        "message": "Статья с таким названием и годом уже есть в журнале",
                    }
                )
            # Статус, снятый клиентом вручную, не воскрешаем.
            if item.status != "skipped":
                item.status = status
            item.problems = problems

        await self._recount(db, job)
        await db.commit()

    async def _existing_keys(
        self, db: AsyncSession, journal_id: int
    ) -> tuple[set[str], set[tuple[str, int | None]]]:
        """DOI и (название, год) статей журнала — база для поиска дубликатов.

        DOI смотрим по всей платформе (он глобально уникален), названия — только
        внутри журнала: одноимённые статьи в разных журналах — норма.
        """
        dois = {
            normalize_doi(d)
            for d in (await db.execute(select(Article.doi).where(Article.doi.isnot(None))))
            .scalars()
            .all()
        }
        dois.discard(None)

        rows = (
            await db.execute(
                select(Article.title, Article.publication_year, Issue.year)
                .join(Issue, Issue.id == Article.issue_id)
                .where(Issue.journal_id == journal_id)
            )
        ).all()
        titles = {
            (normalize_title(t), (py or iy))
            for t, py, iy in rows
            if normalize_title(t)
        }
        return dois, titles  # type: ignore[return-value]

    async def _recount(self, db: AsyncSession, job: ImportJob) -> None:
        rows = (
            await db.execute(
                select(ImportItem.status, func.count(ImportItem.id))
                .where(ImportItem.job_id == job.id)
                .group_by(ImportItem.status)
            )
        ).all()
        totals = {status: int(count) for status, count in rows}
        totals["total"] = sum(totals.values())
        # С PDF — отдельный счётчик: клиенту важно видеть, сколько статей
        # приедет без файла.
        totals["with_pdf"] = int(
            (
                await db.execute(
                    select(func.count(ImportItem.id)).where(
                        ImportItem.job_id == job.id, ImportItem.pdf_url.isnot(None)
                    )
                )
            ).scalar_one()
        )
        job.totals = totals
        if job.status in ("parsing", "draft"):
            job.status = "ready"

    # ---------------------------------------------------------------- PDF
    async def attach_pdf(
        self, db: AsyncSession, job: ImportJob, *, filename: str, url: str
    ) -> ImportItem | None:
        """Связать загруженный PDF со строкой по имени файла из таблицы."""
        name = filename.strip().lower()
        stem = name.rsplit(".", 1)[0]
        items = list(
            (await db.execute(select(ImportItem).where(ImportItem.job_id == job.id)))
            .scalars()
            .all()
        )
        for item in items:
            src = (item.pdf_source or "").strip().lower()
            if not src:
                continue
            # Совпадение по полному имени или по имени без расширения: в таблице
            # пишут и «article1.pdf», и «article1».
            if src == name or src.rsplit(".", 1)[0] == stem:
                item.pdf_url = url
                await self._recount(db, job)
                await db.commit()
                return item
        return None

    # ------------------------------------------------------------ применение
    async def apply(
        self, db: AsyncSession, job: ImportJob, item_ids: Sequence[int] | None = None
    ) -> dict[str, Any]:
        """Создать выпуски и статьи по готовым кандидатам.

        Обрабатываются `pending` и `failed` (последние — чтобы «Повторить
        неудавшиеся» не требовало новой задачи). `duplicate`, `invalid` и
        `skipped` не трогаем: их клиент либо исправил, либо сознательно оставил.
        """
        if job.status == "applying" and not self._is_stale(job):
            raise JobBusy("Импорт уже идёт")

        job.status = "applying"
        job.error = None
        job.heartbeat_at = datetime.now(timezone.utc)
        await db.commit()

        created = failed = 0
        try:
            while True:
                batch = await self._next_batch(db, job, item_ids)
                if not batch:
                    break
                for item in batch:
                    try:
                        await self._create_article(db, job, item)
                        created += 1
                    except Exception as e:  # одна плохая строка не роняет импорт
                        item.status = "failed"
                        item.problems = list(item.problems or []) + [
                            {"field": "", "code": "apply_failed", "message": str(e)[:300]}
                        ]
                        failed += 1
                job.heartbeat_at = datetime.now(timezone.utc)
                await db.commit()

            await self._recount(db, job)
            job.status = "done"
            job.finished_at = datetime.now(timezone.utc)
            await db.commit()
        except Exception as e:
            job.status = "failed"
            job.error = str(e)[:500]
            await db.commit()
            raise

        return {"created": created, "failed": failed}

    def _is_stale(self, job: ImportJob) -> bool:
        if job.heartbeat_at is None:
            return True
        age = datetime.now(timezone.utc) - job.heartbeat_at
        return age.total_seconds() > STALE_AFTER_SECONDS

    async def _next_batch(
        self, db: AsyncSession, job: ImportJob, item_ids: Sequence[int] | None
    ) -> list[ImportItem]:
        stmt = (
            select(ImportItem)
            .where(ImportItem.job_id == job.id, ImportItem.status.in_(("pending", "failed")))
            .order_by(ImportItem.id)
            .limit(APPLY_CHUNK)
        )
        if item_ids:
            stmt = stmt.where(ImportItem.id.in_(list(item_ids)))
        return list((await db.execute(stmt)).scalars().all())

    async def _enrich_from_landing(self, job: ImportJob, item: ImportItem) -> None:
        """Дотянуть со страницы статьи то, чего нет в OAI, и забрать PDF.

        Ни одна неудача здесь не отменяет импорт статьи: старый сайт может
        лежать, отдавать файл только по подписке или вовсе не иметь PDF. Тогда
        статья приезжает с метаданными и замечанием, а не теряется целиком.
        """
        import asyncio

        from src.infrastructure.external.landing import fetch_landing, fetch_pdf
        from src.infrastructure.external.safe_fetch import FetchError
        from src.infrastructure.storage import StorageNotConfigured, public_url, storage

        parsed = dict(item.parsed or {})
        landing_url = parsed.get("landing_url") or item.pdf_source
        if not landing_url:
            return

        problems = list(item.problems or [])
        try:
            landing = await fetch_landing(landing_url)
        except FetchError as e:
            problems.append(
                {"field": "", "code": "landing_failed", "message": f"Страница статьи недоступна: {e}"}
            )
            item.problems = problems
            return

        parsed = merge_landing(parsed, landing)
        item.parsed = parsed
        item.issue_key = issue_key_of(
            parsed.get("publication_year"), parsed.get("volume"), parsed.get("issue")
        )

        if item.pdf_url or not landing.pdf_url:
            if not landing.pdf_url:
                problems.append(
                    {"field": "pdf", "code": "no_pdf", "message": "На странице статьи нет ссылки на PDF"}
                )
                item.problems = problems
            return

        try:
            content = await fetch_pdf(landing.pdf_url)
        except FetchError as e:
            problems.append({"field": "pdf", "code": "pdf_failed", "message": str(e)[:200]})
            item.problems = problems
            return

        key = f"pdfs/{slugify(parsed.get('title') or 'article')[:60]}-{int(time.time() * 1000)}.pdf"
        try:
            await asyncio.to_thread(storage.put, key, content, "application/pdf")
        except StorageNotConfigured:
            problems.append(
                {"field": "pdf", "code": "storage_off", "message": "Хранилище файлов не настроено"}
            )
            item.problems = problems
            return
        item.pdf_url = public_url(key) or key

    async def _create_article(
        self, db: AsyncSession, job: ImportJob, item: ImportItem
    ) -> Article:
        if job.source_type == "oai":
            # Дорогой шаг (два внешних запроса), поэтому здесь, а не при разборе:
            # платим только за статьи, которые действительно импортируют.
            await self._enrich_from_landing(job, item)
        parsed = item.parsed or {}
        issue = await self._get_or_create_issue(db, job, parsed)

        article = Article(
            issue_id=issue.id if issue else None,
            title=parsed.get("title"),
            title_foreign=parsed.get("title_foreign"),
            authors=parsed.get("authors"),
            annotation=parsed.get("annotation"),
            annotation_foreign=parsed.get("annotation_foreign"),
            keywords=parsed.get("keywords"),
            keywords_foreign=parsed.get("keywords_foreign"),
            pages=parsed.get("pages"),
            doi=parsed.get("doi"),
            field_of_science=parsed.get("field_of_science"),
            publication_year=parsed.get("publication_year"),
            data=parse_date(parsed.get("data")),
            pdf=item.pdf_url,
            publication_type="article",
            # Черновик: клиент проверит выпуск и опубликует пачкой. Иначе чужой
            # архив попадёт в витрину непроверенным.
            published=False,
            admin_id=job.created_by,
            slug=await self.articles._unique_slug(db, parsed.get("title") or "article"),
            meta={
                "import": {
                    "job_id": job.id,
                    "source": item.source_key,
                    "source_ref": job.source_ref,
                    "imported_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        db.add(article)
        await db.flush()

        item.article_id = article.id
        item.status = "created"
        return article

    async def _get_or_create_issue(
        self, db: AsyncSession, job: ImportJob, parsed: dict[str, Any]
    ) -> Issue | None:
        """Найти выпуск журнала по году/тому/номеру или создать новый."""
        year = parsed.get("publication_year")
        volume = parsed.get("volume")
        issue_no = parsed.get("issue")
        if year is None and not volume and not issue_no:
            return None  # без ориентиров статью в выпуск не положить

        stmt = select(Issue).where(Issue.journal_id == job.journal_id)
        stmt = stmt.where(Issue.year == year) if year is not None else stmt.where(Issue.year.is_(None))
        stmt = stmt.where(Issue.volume == volume) if volume else stmt.where(Issue.volume.is_(None))
        stmt = stmt.where(Issue.issue == issue_no) if issue_no else stmt.where(Issue.issue.is_(None))
        found = (await db.execute(stmt)).scalars().first()
        if found:
            return found

        title_parts = [p for p in (f"Том {volume}" if volume else None, f"№ {issue_no}" if issue_no else None) if p]
        issue = Issue(
            journal_id=job.journal_id,
            year=year,
            volume=volume,
            issue=issue_no,
            title=(", ".join(title_parts) + (f" ({year})" if year else "")) or (str(year) if year else None),
        )
        db.add(issue)
        await db.flush()
        return issue

    # ---------------------------------------------------------------- прочее
    async def set_item(
        self,
        db: AsyncSession,
        item_id: int,
        *,
        parsed: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> ImportItem:
        item = (
            await db.execute(select(ImportItem).where(ImportItem.id == item_id))
        ).scalars().first()
        if item is None:
            raise JobNotFound("Строка импорта не найдена")
        if item.status == "created":
            raise ImportError_("Строка уже импортирована — правьте саму статью")
        if parsed is not None:
            merged = {**(item.parsed or {}), **parsed}
            # Прогоняем через тот же разбор, что и при загрузке: правка руками
            # не должна обходить нормализацию DOI и проверку года.
            rebuilt, problems = build_parsed(
                {
                    **merged,
                    "year": merged.get("publication_year") or merged.get("year"),
                }
            )
            item.parsed = rebuilt
            item.problems = problems
            item.issue_key = issue_key_of(
                rebuilt.get("publication_year"), rebuilt.get("volume"), rebuilt.get("issue")
            )
            item.status = "invalid" if problems else "pending"
        if status is not None:
            item.status = status
        await db.commit()
        await db.refresh(item)
        return item

    async def cancel(self, db: AsyncSession, job: ImportJob) -> None:
        job.status = "cancelled"
        job.finished_at = datetime.now(timezone.utc)
        await db.commit()

    async def items(
        self,
        db: AsyncSession,
        job_id: int,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ImportItem], int]:
        base = select(ImportItem).where(ImportItem.job_id == job_id)
        if status:
            base = base.where(ImportItem.status == status)
        total = int(
            (
                await db.execute(
                    select(func.count()).select_from(base.subquery())
                )
            ).scalar_one()
        )
        rows = list(
            (
                await db.execute(base.order_by(ImportItem.id).limit(limit).offset(offset))
            )
            .scalars()
            .all()
        )
        return rows, total


def template_csv() -> str:
    """Шаблон таблицы: заголовки + одна строка-пример."""
    example = {
        "title": "Название статьи",
        "title_foreign": "Article title in English",
        "authors": "Каримов А.А.; Petrova E.V.",
        "annotation": "Краткая аннотация статьи",
        "annotation_foreign": "Short abstract in English",
        "keywords": "ключевое слово, ещё одно",
        "keywords_foreign": "keyword, another one",
        "pages": "15-23",
        "doi": "10.1234/example.2024.1",
        "year": "2024",
        "volume": "12",
        "issue": "3",
        "field_of_science": "01.04.00 Физика",
        "pdf_filename": "article-1.pdf",
    }
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(TEMPLATE_COLUMNS))
    writer.writeheader()
    writer.writerow(example)
    return buf.getvalue()

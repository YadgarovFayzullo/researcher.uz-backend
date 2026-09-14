"""Метаданные статьи из самого PDF — для загрузки папки без таблицы.

Разведку делал `scripts/probe_pdf_meta.py` (проверено на живых статьях двух
журналов); здесь то же самое в боевом виде. Два слоя:

* **regex** — то, что стабильно лежит в колонтитуле и подвале: ISSN, том,
  номер, номера первой и последней страниц. Бесплатно и без сети.
* **модель** — шапка статьи: заголовок, авторы с аффилиациями, аннотация,
  ключевые слова, по языкам. Regex тут бессилен: у одного журнала ФИО идут
  после заголовка, у другого сначала кафедра, а экстрактор к тому же отдаёт
  текст не в визуальном порядке (подвал раньше заголовка).

Что сознательно НЕ извлекается:

* **год, том и номер** — они берутся из выпуска, в который редактор грузит
  папку. Четырёхзначные числа первой страницы чаще всего оказываются годами
  импакт-фактора («SJIF 2024»); том и номер из колонтитула только сверяются с
  выпуском и дают предупреждение при расхождении;
* **DOI** — его присваивает платформа при публикации, в исходных файлах его
  нет, а DOI, найденный на первых страницах, бывает ссылкой из списка
  литературы.

Всё, что вернула модель, сверяется с текстом файла ТЕМИ ЖЕ мерками, что потом
применит автопроверка выпуска (`issue_checks`). Иначе ошибка распознавания
доехала бы до проверки как major и погасила выпуск честного редактора — лучше
показать её в списке загрузки, где поле можно поправить.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.core.config import settings
from src.domain import issue_checks
from src.infrastructure.external import llm
from src.infrastructure.pdf_text import PDFIUM_LOCK, clean_text

# Сколько текста храним у кандидата и отдаём модели. Три страницы вёрстки —
# 6-8 тысяч знаков; потолок отсекает патологию вроде PDF, где весь текст
# лежит одной строкой.
MAX_TEXT_CHARS = 20000

# Строгость замечаний к кандидату. Блокирующее не даёт создать статью, пока
# редактор не поправит поле; предупреждение только показывается.
BLOCKING = "blocking"
WARNING = "warning"

# Статусы распознавания (`import_items.raw.extract.status`).
EXTRACT_QUEUED = "queued"      # файл принят, модель ещё не отвечала
EXTRACT_DONE = "done"
EXTRACT_FAILED = "failed"      # модель не ответила за все попытки
EXTRACT_NO_TEXT = "no_text"    # скан без текстового слоя
EXTRACT_NO_MODEL = "no_model"  # ключ модели не настроен


class PdfUnreadable(Exception):
    """Файл не открылся как PDF."""


@dataclass
class PdfHead:
    """Первые страницы файла — всё, что нужно для метаданных."""

    pages: list[str]
    tail: str
    info: dict[str, str]
    total_pages: int

    @property
    def has_text(self) -> bool:
        return any(page.strip() for page in self.pages)


def read_head(pdf_bytes: bytes, *, max_pages: int) -> PdfHead:
    """Текст первых `max_pages` страниц, последней страницы и свойства файла.

    Последняя страница нужна ради номера в подвале: вместе с номером первой
    он даёт диапазон страниц статьи в выпуске.
    """
    import pypdfium2 as pdfium

    def page_text(doc: Any, index: int) -> str:
        page = doc[index]
        textpage = page.get_textpage()
        try:
            text = textpage.get_text_range() or ""
        finally:
            textpage.close()
            page.close()
        return text.replace("\r\n", "\n").replace("\r", "\n")

    # PDFium не потокобезопасен — см. PDFIUM_LOCK в pdf_text.py.
    with PDFIUM_LOCK:
        try:
            doc = pdfium.PdfDocument(pdf_bytes)
        except Exception as e:  # битый файл: pypdfium2 бросает PdfiumError
            raise PdfUnreadable(str(e)) from e
        try:
            total = len(doc)
            head = [page_text(doc, i) for i in range(min(max_pages, total))]
            if total > len(head):
                tail = page_text(doc, total - 1)
            else:
                tail = head[-1] if head else ""
            info = {
                key: (doc.get_metadata_value(key) or "")
                for key in ("Title", "Author", "CreationDate", "Producer")
            }
        finally:
            doc.close()
    return PdfHead(pages=head, tail=tail, info=info, total_pages=total)


def pages_for_storage(head: PdfHead) -> list[str]:
    """Текст страниц без переносов вёрстки, в пределах общего потолка."""
    out: list[str] = []
    budget = MAX_TEXT_CHARS
    for page in head.pages:
        if budget <= 0:
            break
        text = clean_text(page)[:budget]
        out.append(text)
        budget -= len(text)
    return out


# --------------------------------------------------------------------------
# Слой regex
# --------------------------------------------------------------------------

# «Volume 4, Issue 8», «Vol. 4 No. 8», «Том 4, № 8», «4-jild, 7-son».
_RE_VOLUME = re.compile(
    r"\b(?:volume|vol\.?|том|jild)\s*[-–—:]?\s*(\d{1,3})|(\d{1,3})\s*[-–—]\s*jild\b",
    re.IGNORECASE,
)
_RE_ISSUE = re.compile(
    r"(?:\b(?:issue|no\.?|номер|son)|№)\s*[-–—:]?\s*(\d{1,3})|(\d{1,3})\s*[-–—]\s*son\b",
    re.IGNORECASE,
)
# Строка из одного числа — номер страницы в колонтитуле или подвале.
_RE_LONE_NUMBER = re.compile(r"(?m)^\s*(\d{1,5})\s*$")


def _first_group(match: re.Match[str] | None) -> str:
    """Первая непустая группа: в паттернах два порядка («4-jild»)."""
    if not match:
        return ""
    return next((g for g in match.groups() if g), "")


def _lone_numbers(text: str) -> list[int]:
    # Годы отдельной строкой (год выпуска в колонтитуле) номером страницы не
    # бывают, а в пару с чем-нибудь сложиться могут.
    return [n for n in map(int, _RE_LONE_NUMBER.findall(text or "")) if not 1900 <= n <= 2100]


def page_span(head: PdfHead) -> tuple[int, int] | None:
    """Первая и последняя страница статьи в выпуске по номерам в подвалах.

    Кандидатов на странице бывает несколько (том и номер тоже стоят отдельной
    строкой), поэтому принимаем только пару, разница в которой совпадает с
    числом страниц файла, — иначе ничего. Пустые страницы лучше выдуманных:
    их редактор допишет, а неверные пересеклись бы с соседней статьёй.
    """
    total = head.total_pages
    if total <= 0 or not head.pages:
        return None
    firsts = _lone_numbers(head.pages[0])
    if total == 1:
        return (firsts[0], firsts[0]) if len(set(firsts)) == 1 else None
    # На первой странице номер часто не печатают — тогда выводим его из номера
    # второй. Только если последняя страница другая: иначе «пара» сложилась бы
    # из двух чисел одной и той же страницы.
    if len(head.pages) > 1 and total > 2:
        firsts += [n - 1 for n in _lone_numbers(head.pages[1])]
    lasts = set(_lone_numbers(head.tail))
    for start in dict.fromkeys(firsts):
        if start >= 1 and start + total - 1 in lasts:
            return start, start + total - 1
    return None


def regex_layer(head: PdfHead) -> dict[str, Any]:
    """То, что лежит в колонтитуле и подвале и не нуждается в модели."""
    first = head.pages[0] if head.pages else ""
    header_line = next(
        (line for line in first.splitlines() if _RE_VOLUME.search(line) or _RE_ISSUE.search(line)),
        "",
    )
    span = page_span(head)
    pages = ""
    if span:
        pages = str(span[0]) if span[0] == span[1] else f"{span[0]}-{span[1]}"
    return {
        "pages": pages,
        "volume": _first_group(_RE_VOLUME.search(header_line)),
        "issue": _first_group(_RE_ISSUE.search(header_line)),
        "issns": sorted(issue_checks.find_issns("\n".join(head.pages))),
        "header_line": header_line.strip()[:300],
    }


# --------------------------------------------------------------------------
# Слой модели
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
Ты разбираешь шапку научной статьи из журнала (Узбекистан, СНГ) и возвращаешь \
метаданные строго по схеме.

Правила:
* Копируй текст ДОСЛОВНО из документа. Не переводи, не сокращай, не \
переписывай, не исправляй опечатки автора. Каждое значение потом сверяется с \
текстом файла, и перефразированное будет отклонено.
* Одна и та же статья часто напечатана на нескольких языках подряд (английский, \
узбекский, русский) — каждая версия это отдельный элемент versions, в том \
порядке, в котором они идут в файле.
* Текст приходит в порядке извлечения, а не в визуальном: колонтитул и номер \
страницы могут стоять ПЕРЕД заголовком. Ориентируйся на смысл, а не на порядок \
строк.
* Заголовок часто разбит переносами на несколько строк — склей в одну строку.
* Аннотация может быть структурированной (ВВЕДЕНИЕ / ЦЕЛЬ / МЕТОДЫ / ВЫВОДЫ) — \
возвращай её целиком, вместе с этими подзаголовками.
* Автор и его место работы бывают в любом порядке: где-то ФИО, потом вуз, \
где-то сначала кафедра и должность, потом ФИО. Разбирай по смыслу. В name — \
только ФИО, без должности и учёной степени.
* Название журнала бери из колонтитула, не путай с заголовком статьи.
* Если поля в документе нет — пустая строка (или пустой список). Ничего не \
придумывай и не выводи по догадке.
* В notes пиши замечания: нечитаемый текст (скан, кракозябры вместо букв), \
подозрение, что языковые версии не соответствуют друг другу, отсутствие \
обязательных полей.\
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "journal_title": {"type": "string"},
        "udc": {"type": "string"},
        "versions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "language": {"type": "string", "enum": ["en", "ru", "uz", "uz-cyrl", "other"]},
                    "title": {"type": "string"},
                    "abstract": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["language", "title", "abstract", "keywords"],
                "additionalProperties": False,
            },
        },
        "authors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "affiliation": {"type": "string"},
                    "email": {"type": "string"},
                },
                "required": ["name", "affiliation", "email"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["journal_title", "udc", "versions", "authors", "notes"],
    "additionalProperties": False,
}


def model_name() -> str:
    """Чем распознавали — пишется в кандидата, чтобы разбирать жалобы."""
    if llm.provider() == "anthropic":
        return f"anthropic:{settings.IMPORT_EXTRACT_MODEL}"
    return llm.active_model()


async def llm_layer(pages: list[str]) -> dict[str, Any]:
    """Шапка статьи через модель. Ошибку провайдера пробрасывает наверх."""
    document = "\n\n".join(
        f"=== СТРАНИЦА {i + 1} ===\n{text}" for i, text in enumerate(pages) if text.strip()
    )
    return await llm.structured(
        system=SYSTEM_PROMPT,
        user=document,
        schema=SCHEMA,
        # Разбор шапки — перенос текста в поля, а не рассуждение: для Claude
        # берём дешёвую модель без thinking. У Gemini и OpenRouter модель
        # задаётся их собственными переменными окружения.
        anthropic_model=settings.IMPORT_EXTRACT_MODEL,
        thinking=False,
    )


def _one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _author_name(name: Any) -> str:
    # Запятая в `articles.authors` разделяет авторов, внутри имени ей не место:
    # «Karimov, A.» развалилось бы на двух человек.
    return _one_line(str(name or "").replace(",", " "))


def _keywords(version: dict[str, Any] | None) -> list[str]:
    return [_one_line(k) for k in ((version or {}).get("keywords") or []) if _one_line(k)]


def to_fields(parsed: dict[str, Any]) -> dict[str, Any]:
    """Ответ модели → поля кандидата в терминах `articles`.

    Языковых версий бывает три, а слотов в таблице два (`title`/`title_foreign`),
    поэтому третья и далее уходят в `metadata.pdf_meta` — схему не трогаем.
    Основной считаем версию, идущую в файле первой (язык самой статьи);
    иностранной — английскую, а если основная сама английская, следующую.
    """
    versions = [v for v in (parsed.get("versions") or []) if _one_line(v.get("title"))]
    primary = versions[0] if versions else {}
    rest = versions[1:]
    foreign: dict[str, Any] | None = None
    if primary.get("language") != "en":
        foreign = next((v for v in rest if v.get("language") == "en"), None)
    if foreign is None and rest:
        foreign = rest[0]
    extra = [v for v in rest if v is not foreign]

    authors = [a for a in (parsed.get("authors") or []) if _author_name(a.get("name"))]
    return {
        "title": _one_line(primary.get("title")) or None,
        "title_foreign": _one_line((foreign or {}).get("title")) or None,
        "annotation": str(primary.get("abstract") or "").strip() or None,
        "annotation_foreign": str((foreign or {}).get("abstract") or "").strip() or None,
        "keywords": ", ".join(_keywords(primary)) or None,
        "keywords_foreign": ", ".join(_keywords(foreign)) or None,
        "authors": ", ".join(_author_name(a.get("name")) for a in authors) or None,
        "pdf_meta": {
            "language": primary.get("language"),
            "foreign_language": (foreign or {}).get("language"),
            "journal_title": _one_line(parsed.get("journal_title")) or None,
            "udc": _one_line(parsed.get("udc")) or None,
            "authors_detailed": [
                {
                    "name": _author_name(a.get("name")),
                    "affiliation": _one_line(a.get("affiliation")) or None,
                    "email": _one_line(a.get("email")) or None,
                }
                for a in authors
            ],
            "extra_languages": [
                {
                    "language": v.get("language"),
                    "title": _one_line(v.get("title")),
                    "abstract": str(v.get("abstract") or "").strip(),
                    "keywords": _keywords(v),
                }
                for v in extra
            ],
            "notes": [_one_line(n) for n in (parsed.get("notes") or []) if _one_line(n)],
        },
    }


# --------------------------------------------------------------------------
# Сверка кандидата с файлом
# --------------------------------------------------------------------------


def _problem(field: str, code: str, message: str, severity: str = BLOCKING) -> dict[str, str]:
    return {"field": field, "code": code, "message": message, "severity": severity}


def _same_number(a: Any, b: Any) -> bool:
    return str(a or "").strip().lstrip("0") == str(b or "").strip().lstrip("0")


def check_fields(
    fields: dict[str, Any],
    *,
    pages_text: list[str],
    total_pages: int,
    extract: dict[str, Any],
    regex: dict[str, Any],
    issue_volume: str | None,
    issue_number: str | None,
    journal_issns: set[str],
) -> list[dict[str, str]]:
    """Замечания к кандидату, которых не видит `build_parsed`.

    Пороги повторяют автопроверку выпуска: что там major и поправимо
    редактором, здесь блокирует; что поправить нельзя (чужой ISSN в файле) —
    только предупреждает, решать это будет автопроверка и владелец.

    Если редактор отметил «всё верно» (`confirmed`), блокирующие сверки с
    текстом становятся предупреждениями: текстовый слой бывает битым
    (кракозябры вместо кириллицы), и честная правка не должна застревать
    навсегда. Последнее слово тогда за автопроверкой выпуска.
    """
    problems: list[dict[str, str]] = []
    content = WARNING if fields.get("confirmed") else BLOCKING
    status = extract.get("status")

    if status == EXTRACT_NO_TEXT:
        problems.append(
            _problem(
                "pdf",
                "scan",
                "В PDF нет текстового слоя (скан): данные не распознать, заполните их "
                "вручную. Автопроверка такую статью сама не опубликует — решит владелец.",
                WARNING,
            )
        )
    elif status == EXTRACT_NO_MODEL:
        problems.append(
            _problem(
                "",
                "extract_off",
                "Автораспознавание не настроено — заполните название и авторов вручную.",
                WARNING,
            )
        )
    elif status == EXTRACT_FAILED:
        problems.append(
            _problem(
                "",
                "extract_failed",
                f"Не удалось распознать автоматически: {extract.get('error') or 'нет ответа'}",
                WARNING,
            )
        )

    if not fields.get("authors") and status != EXTRACT_QUEUED:
        problems.append(_problem("authors", "authors_required", "Не указаны авторы"))

    haystack = issue_checks.normalize("\n".join(pages_text))
    if haystack:
        title_words = issue_checks.words(fields.get("title"))
        if title_words:
            hits = sum(1 for word in title_words if word in haystack)
            ratio = hits / len(title_words)
            if ratio < issue_checks.TITLE_MAJOR_RATIO:
                problems.append(
                    _problem(
                        "title",
                        "title_not_in_pdf",
                        f"Название почти не встречается в тексте PDF (совпало слов: "
                        f"{hits} из {len(title_words)}) — проверьте, тот ли это файл",
                        content,
                    )
                )
            elif ratio < issue_checks.TITLE_MINOR_RATIO:
                problems.append(
                    _problem(
                        "title",
                        "title_partial",
                        f"Название совпадает с PDF частично ({hits} из {len(title_words)} слов)",
                        WARNING,
                    )
                )

        surnames = issue_checks.author_surnames(fields.get("authors"))
        if surnames and not any(surname in haystack for surname in surnames):
            problems.append(
                _problem(
                    "authors",
                    "authors_not_in_pdf",
                    "Ни одна фамилия автора не найдена в тексте PDF — проверьте авторов",
                    content,
                )
            )

        pdf_issns = set(regex.get("issns") or [])
        if journal_issns and pdf_issns and not (pdf_issns & journal_issns):
            problems.append(
                _problem(
                    "journal",
                    "foreign_issn",
                    f"В PDF указан ISSN {', '.join(sorted(pdf_issns)[:3])}, а у журнала "
                    f"{', '.join(sorted(journal_issns))}. Если это статья другого издания, "
                    "автопроверка закроет выпуск.",
                    WARNING,
                )
            )

    span = issue_checks.page_range(fields.get("pages"))
    if span and total_pages and span == (1, total_pages):
        problems.append(
            _problem(
                "pages",
                "pages_file_volume",
                f"Страницы «{fields.get('pages')}» совпадают с объёмом файла — укажите "
                "страницы статьи в выпуске",
                content,
            )
        )
    elif not fields.get("pages") and status != EXTRACT_QUEUED:
        problems.append(
            _problem(
                "pages",
                "pages_missing",
                "Номера страниц в файле не найдены — впишите диапазон страниц в выпуске",
                WARNING,
            )
        )

    pdf_volume, pdf_issue = regex.get("volume"), regex.get("issue")
    mismatch = (pdf_volume and issue_volume and not _same_number(pdf_volume, issue_volume)) or (
        pdf_issue and issue_number and not _same_number(pdf_issue, issue_number)
    )
    if mismatch:
        problems.append(
            _problem(
                "issue",
                "issue_mismatch",
                f"В колонтитуле PDF: том {pdf_volume or '—'}, № {pdf_issue or '—'}; "
                f"выпуск на платформе: том {issue_volume or '—'}, № {issue_number or '—'}",
                WARNING,
            )
        )
    return problems

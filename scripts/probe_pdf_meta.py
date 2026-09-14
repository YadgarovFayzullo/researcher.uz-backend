"""Пробник: что удаётся вытащить из PDF статьи без таблицы метаданных.

Не часть импорта и ничего не пишет в базу — это разведка перед тем, как делать
загрузку папки со статьями (см. `import-integration.md`). На вход папка или
отдельные файлы, на выход JSON по каждой статье плюс сводка «что нашлось».

Два слоя, как и задумано для будущей фичи:

* **regex** — то, что стабильно лежит в колонтитуле и подвале: ISSN, том, номер,
  номера первой и последней страниц, DOI, e-mail. Работает без сети и бесплатно.
* **LLM** — шапка статьи: заголовок, авторы с аффилиациями, аннотация, ключевые
  слова, и раскладка по языкам (в PDF их бывает три). Regex здесь бессилен:
  у одного журнала ФИО идут после заголовка, у другого сначала кафедра, а
  экстрактор к тому же отдаёт текст не в визуальном порядке.

Год публикации сознательно НЕ угадывается по тексту: в проверенных файлах
единственные четырёхзначные числа на первой странице — это годы импакт-фактора
(«SJIF 2024»), и наивный поиск даёт неверный ответ. Скрипт показывает все
кандидатуры (шапка, дата создания файла) и оставляет выбор человеку.

    PYTHONPATH=. .venv/bin/python scripts/probe_pdf_meta.py ~/pdfs
    ... --no-llm             # только regex, без обращения к API и без денег
    ... --limit 10 --out /tmp/probe

Нужен `pip install anthropic` в venv и ключ в ANTHROPIC_API_KEY (в
requirements.txt пакет намеренно не добавлен — это разведка, а не прод).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# Датированный снимок, а не алиас `claude-haiku-4-5`: разбор метаданных должен
# вести себя одинаково от прогона к прогону, иначе непонятно, что менялось —
# наш промпт или модель под алиасом.
MODEL = "claude-haiku-4-5-20251001"

# $ за миллион токенов: (вход, выход). Нужно только чтобы печатать стоимость
# прогона — если модель не из списка, показываем прочерк вместо выдуманной цены.
PRICES = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
}

# Сколько страниц отдаём модели. Три — потому что многоязычные журналы печатают
# по странице на язык; дальше идёт тело статьи, метаданных там уже нет.
DEFAULT_MAX_PAGES = 3

_RE_ISSN = re.compile(r"ISSN\s*(?:\((?:E|online|печ\w*)\)\s*)?:?\s*(\d{4}-\d{3}[\dXx])", re.I)
_RE_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_RE_UDC = re.compile(r"(?:УДК|UDK|UO['’ʻ`ʼ]?K)\s*[:\s]\s*([\d.\-+():'\s]{3,40})", re.I)
# «volume 4, issue 8», «Volume-4, Issue-7», «Том 4, №8», «4-jild, 7-son».
_RE_VOLUME = re.compile(r"(?:volume|vol\.?|том|jild)\s*[-–—:]?\s*(\d{1,3})|(\d{1,3})\s*[-–—]\s*jild", re.I)
_RE_ISSUE = re.compile(r"(?:issue|no\.?|№|номер|son)\s*[-–—:]?\s*(\d{1,3})|(\d{1,3})\s*[-–—]\s*son", re.I)
# Строка подвала/колонтитула, состоящая из одного числа, — номер страницы.
_RE_LONE_NUMBER = re.compile(r"(?m)^\s*(\d{1,5})\s*$")

SYSTEM_PROMPT = """\
Ты разбираешь шапку научной статьи из журнала (Узбекистан, СНГ) и возвращаешь \
метаданные строго по схеме.

Правила:
* Копируй текст ДОСЛОВНО из документа. Не переводи, не сокращай, не \
переписывай, не исправляй опечатки автора.
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
где-то сначала кафедра и должность, потом ФИО. Разбирай по смыслу.
* Название журнала бери из колонтитула, не путай с заголовком статьи.
* Если поля в документе нет — пустая строка (или пустой список). Ничего не \
придумывай и не выводи по догадке.
* В notes пиши замечания: нечитаемый текст (скан), подозрение, что языковые \
версии не соответствуют друг другу, отсутствие обязательных полей.\
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "journal_title": {"type": "string"},
        "udc": {"type": "string"},
        "doi": {"type": "string"},
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
    "required": ["journal_title", "udc", "doi", "versions", "authors", "notes"],
    "additionalProperties": False,
}


# ------------------------------------------------------------------ извлечение


def read_pages(pdf_bytes: bytes, max_pages: int) -> tuple[list[str], str, dict[str, str]]:
    """Первые страницы, последняя страница и служебные метаданные PDF."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        total = len(doc)
        head = [doc[i].get_textpage().get_text_range() for i in range(min(max_pages, total))]
        tail = doc[total - 1].get_textpage().get_text_range() if total else ""
        info = {
            key: (doc.get_metadata_value(key) or "")
            for key in ("Title", "Author", "CreationDate", "Producer")
        }
        info["Pages"] = str(total)
        return head, tail, info
    finally:
        doc.close()


# Наукометрия в колонтитуле: «ResearchBib Impact Factor: 6,4 / 2024 SJIF 2024
# - 5.073». Годы здесь чужие — относятся к метрике, а не к выпуску, и наивный
# поиск четырёхзначного числа берёт именно их. Вырезаем до поиска года.
_RE_METRIC_NOISE = re.compile(
    r"(?:research\s*bib|impact\s*factor|sjif|oajif|gif|jif)\s*[:\-–—]?\s*[\d.,/\s]*", re.I
)


def _first_group(match: re.Match[str] | None) -> str:
    """Первая непустая группа: в паттернах два варианта порядка («4-jild»)."""
    if not match:
        return ""
    return next((g for g in match.groups() if g), "")


def _group(regex: re.Pattern[str], text: str, group: int = 1) -> str:
    match = regex.search(text)
    return match.group(group).strip() if match else ""


def page_range(first_page: str, last_page: str, total: int) -> tuple[str, str]:
    """Номера первой и последней страниц из подвалов.

    Кандидатов на странице бывает несколько (год, номер тома тоже стоят
    отдельной строкой), поэтому берём пару, разница в которой совпадает с
    числом страниц в файле, — так ошибиться почти невозможно.
    """
    firsts = [int(n) for n in _RE_LONE_NUMBER.findall(first_page)]
    lasts = [int(n) for n in _RE_LONE_NUMBER.findall(last_page)]
    if total == 1:
        return (str(firsts[0]), str(firsts[0])) if firsts else ("", "")
    for start in firsts:
        for end in lasts:
            if end - start == total - 1:
                return str(start), str(end)
    return (str(firsts[0]) if firsts else "", str(lasts[0]) if lasts else "")


def regex_layer(head: list[str], tail: str, info: dict[str, str]) -> dict[str, Any]:
    """То, что лежит в колонтитуле и подвале и не нуждается в модели."""
    first = head[0] if head else ""
    blob = "\n".join(head)
    start, end = page_range(first, tail, int(info.get("Pages") or 1))

    # Год: только из явных источников. В тексте первой страницы четырёхзначные
    # числа чаще всего оказываются годами импакт-фактора, а не годом выпуска.
    creation = info.get("CreationDate", "")
    year_from_file = creation[2:6] if creation.startswith("D:") and creation[2:6].isdigit() else ""
    header_line = next(
        (ln for ln in first.splitlines() if _RE_VOLUME.search(ln) or _RE_ISSUE.search(ln)),
        "",
    )
    year_from_header = _group(
        re.compile(r"\b((?:19|20)\d{2})\b"), _RE_METRIC_NOISE.sub(" ", header_line)
    )

    return {
        "issn": _group(_RE_ISSN, blob),
        "volume": _first_group(_RE_VOLUME.search(header_line or first)),
        "issue": _first_group(_RE_ISSUE.search(header_line or first)),
        "page_start": start,
        "page_end": end,
        "pages": f"{start}-{end}" if start and end else "",
        "doi": _group(_RE_DOI, blob, group=0),
        "udc": _group(_RE_UDC, blob),
        "emails": sorted(set(_RE_EMAIL.findall(blob))),
        "year_candidates": {
            "from_header": year_from_header,
            "from_pdf_created": year_from_file,
        },
        "header_line": header_line.strip(),
        "total_pages": info.get("Pages", ""),
    }


# ------------------------------------------------------------------------ LLM


def llm_layer(client: Any, head: list[str], model: str) -> tuple[dict[str, Any], Any]:
    """Шапка статьи через модель. Возвращает разбор и usage для подсчёта денег."""
    document = "\n\n".join(
        f"=== СТРАНИЦА {i + 1} ===\n{text}" for i, text in enumerate(head) if text.strip()
    )
    response = client.messages.create(
        model=model,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": document}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("модель отказалась разбирать документ")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("ответ не поместился в max_tokens — статья слишком объёмная")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text), response.usage


def to_article_fields(parsed: dict[str, Any], regex: dict[str, Any]) -> dict[str, Any]:
    """Разложить разбор по колонкам `articles`.

    Языковых версий бывает три, а слотов в таблице два (`title`/`title_foreign`),
    поэтому третий и далее уходят в `metadata` — так решено, схему не трогаем.
    """
    versions = parsed.get("versions") or []
    primary = versions[0] if versions else {}
    foreign = versions[1] if len(versions) > 1 else {}
    authors = parsed.get("authors") or []

    return {
        "title": primary.get("title", ""),
        "title_foreign": foreign.get("title", ""),
        "annotation": primary.get("abstract", ""),
        "annotation_foreign": foreign.get("abstract", ""),
        "keywords": ", ".join(primary.get("keywords") or []),
        "keywords_foreign": ", ".join(foreign.get("keywords") or []),
        "authors": "; ".join(a.get("name", "") for a in authors if a.get("name")),
        "pages": regex["pages"],
        "doi": regex["doi"] or parsed.get("doi", ""),
        "publication_year": regex["year_candidates"]["from_header"]
        or regex["year_candidates"]["from_pdf_created"],
        "issue_key": "|".join(
            filter(
                None,
                [
                    regex["year_candidates"]["from_header"],
                    regex["volume"],
                    regex["issue"],
                ],
            )
        ),
        "metadata": {
            "journal_title": parsed.get("journal_title", ""),
            "issn": regex["issn"],
            "udc": parsed.get("udc") or regex["udc"],
            "authors_detailed": authors,
            "extra_languages": versions[2:],
        },
    }


# ----------------------------------------------------------------------- вывод


def collect_pdfs(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*.pdf") if p.is_file()))
        elif path.is_file():
            files.append(path)
        else:
            print(f"! не найдено: {path}", file=sys.stderr)
    return files


def describe(result: dict[str, Any]) -> str:
    """Одна строка сводки: что нашлось, чего нет."""
    fields = result["article"]
    missing = [
        name
        for name in ("title", "authors", "annotation", "keywords", "pages", "publication_year")
        if not fields.get(name)
    ]
    langs = ",".join(v.get("language", "?") for v in result["parsed"].get("versions", []))
    status = "OK" if not missing else "нет: " + ",".join(missing)
    return f"  языки={langs or '—'}  выпуск={fields['issue_key'] or '—'}  стр.={fields['pages'] or '—'}  {status}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="папка со статьями или отдельные PDF")
    parser.add_argument("--out", default="probe_out", help="куда складывать JSON (по умолчанию ./probe_out)")
    parser.add_argument("--limit", type=int, default=0, help="разобрать только первые N файлов")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--no-llm", action="store_true", help="только regex — без сети и без расходов")
    args = parser.parse_args()

    files = collect_pdfs(args.paths)
    if args.limit:
        files = files[: args.limit]
    if not files:
        print("PDF не найдены", file=sys.stderr)
        return 1

    client = None
    if not args.no_llm:
        try:
            import anthropic
        except ImportError:
            print("нужен пакет anthropic: .venv/bin/pip install anthropic", file=sys.stderr)
            return 1
        if not os.getenv("ANTHROPIC_API_KEY"):
            print("не задан ANTHROPIC_API_KEY", file=sys.stderr)
            return 1
        client = anthropic.Anthropic()

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    tokens_in = tokens_out = 0
    failed = 0
    print(f"Файлов: {len(files)}; модель: {'—' if args.no_llm else args.model}\n")

    for path in files:
        print(f"{path.name}")
        try:
            head, tail, info = read_pages(path.read_bytes(), args.max_pages)
        except Exception as exc:  # noqa: BLE001 — пробнику важно дойти до конца пачки
            print(f"  ! не читается: {exc}")
            failed += 1
            continue

        if not any(t.strip() for t in head):
            print("  ! текстового слоя нет — скан, метаданные не извлечь")
            failed += 1
            continue

        regex = regex_layer(head, tail, info)
        parsed: dict[str, Any] = {"versions": [], "authors": [], "notes": []}

        if client is not None:
            try:
                parsed, usage = llm_layer(client, head, args.model)
                tokens_in += usage.input_tokens
                tokens_out += usage.output_tokens
            except Exception as exc:  # noqa: BLE001
                print(f"  ! модель не разобрала: {exc}")
                failed += 1
                continue

        result = {
            "file": str(path),
            "pdf_info": info,
            "regex": regex,
            "parsed": parsed,
            "article": to_article_fields(parsed, regex),
        }
        (out_dir / f"{path.stem}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(describe(result))
        for note in parsed.get("notes") or []:
            print(f"  замечание: {note}")

    print(f"\nРазобрано: {len(files) - failed}/{len(files)}; JSON в {out_dir}")
    if tokens_in or tokens_out:
        price = PRICES.get(args.model)
        cost = (
            f"${tokens_in / 1e6 * price[0] + tokens_out / 1e6 * price[1]:.4f}"
            if price
            else "— (цена модели неизвестна)"
        )
        print(f"Токенов: вход {tokens_in}, выход {tokens_out}; стоимость {cost}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Извлечение текста из PDF — основа проверки на заимствования.

Рендерер обложек (`covers.py`) уже тянет pypdfium2, он же умеет отдавать текст,
поэтому новых зависимостей не нужно.

Что здесь важно, кроме самого извлечения:

* **Сканы.** Часть архивных статей — картинки без текстового слоя. Такой PDF
  отдаёт пустую строку, и это не ошибка, а диагноз: проверять нечего, надо
  сказать редактору прямо, а не показывать «0% заимствований».
* **Переносы.** В вёрстке слова разорваны дефисом на конце строки
  («иссле-\\nдование»). Без склейки шинглы разъезжаются, и совпадение с тем же
  текстом в другой вёрстке теряется.
* **Списки литературы.** Совпадения в библиографии — норма, а не плагиат: одни
  и те же источники цитируют десятки статей. Хвост после «Список литературы» /
  «References» отрезаем, иначе процент заимствований будет завышен у всех.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Больше этого в одну проверку не берём: диссертация на 300 страниц раздувает и
# извлечение, и число шинглов, а для решения хватает основного объёма.
MAX_PAGES = 200

# Заголовки, после которых начинается библиография (кириллица, латиница, узбекский).
_REFERENCES_HEADINGS = re.compile(
    r"^[\s\d.]*(?:"
    r"список\s+(?:использованн\w+\s+)?(?:литератур\w+|источник\w+)"
    r"|литература"
    r"|библиографи\w*(?:\s+список)?"
    r"|(?:использованные\s+)?источники"
    r"|references?"
    r"|reference\s+list"
    r"|bibliography"
    # Узбекский: «Foydalanilgan adabiyotlar roʻyxati», «Adabiyotlar», «Manbalar».
    # Апостроф в «roʻyxati» пишут пятью разными символами — принимаем любой.
    r"|(?:foydalanilgan\s+)?adabiyotlar(?:\s+ro['’ʻ`ʼ]?yxati)?"
    r"|manbalar(?:\s+ro['’ʻ`ʼ]?yxati)?"
    r")\s*[:.]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Перенос слова: «иссле-\nдование» → «исследование». Требуем букву с обеих
# сторон, иначе склеим настоящий дефис в «одно-, двух- и трёхмерный».
_HYPHEN_BREAK = re.compile(r"(\w)[-‐‑]\s*\n\s*(\w)", re.UNICODE)


def extract_text(pdf_bytes: bytes, *, max_pages: int = MAX_PAGES) -> str:
    """Текст из PDF. Пустая строка — текстового слоя нет (скан)."""
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover — зависимость есть в requirements
        logger.warning("pypdfium2 не установлен — текст из PDF не извлечь")
        return ""

    doc = None
    parts: list[str] = []
    try:
        doc = pdfium.PdfDocument(pdf_bytes)
        for index in range(min(len(doc), max_pages)):
            page = doc[index]
            textpage = page.get_textpage()
            try:
                parts.append(textpage.get_text_range() or "")
            finally:
                textpage.close()
                page.close()
    except Exception:
        logger.exception("Не удалось извлечь текст из PDF")
        return ""
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass

    return "\n".join(parts)


def strip_references(text: str) -> str:
    """Отрезать список литературы — совпадения в нём не плагиат.

    Ищем заголовок в последней трети документа: слова «references» встречаются
    и в середине текста (например, в аннотации), а библиография всегда в конце.
    """
    if not text:
        return text
    threshold = int(len(text) * 0.6)
    cut: int | None = None
    for match in _REFERENCES_HEADINGS.finditer(text):
        if match.start() >= threshold:
            cut = match.start()
            break
    return text[:cut] if cut else text


def clean_text(text: str) -> str:
    """Убрать переносы строк-дефисов и лишние пробелы, сохранив границы абзацев."""
    if not text:
        return ""
    # PDF отдаёт CRLF; без нормализации «\r» остаётся в конце строки и мешает
    # искать заголовки построчно.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    # Мягкий перенос (U+00AD) в PDF встречается как невидимый символ.
    text = text.replace("­", "")
    # Одиночные переводы строк внутри абзаца — это вёрстка, а не смысл.
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{2,}", "\n\n", text)
    return text.strip()


# Колонтитул повторяется на каждой странице. Короткая строка, встретившаяся
# трижды и чаще, — это почти наверняка он, а не осмысленный текст.
_RUNNING_HEAD_MIN_REPEATS = 3
_RUNNING_HEAD_MAX_LEN = 120


def strip_running_heads(text: str) -> str:
    """Убрать колонтитулы и номера страниц.

    Зачем: в шапке статьи стоит название журнала, ISSN и номер выпуска
    («INTER STUDY ISSN 3030-9575 … 2026, volume 2, issue 2»), и оно повторяется
    на каждой странице КАЖДОЙ статьи журнала. Без вычистки все статьи одного
    издания выглядят как заимствования друг у друга: в базе нашёлся колонтитул,
    сидящий сразу в 1863 статьях.
    """
    if not text:
        return text

    lines = text.split("\n")
    counts: dict[str, int] = {}
    for line in lines:
        stripped = line.strip()
        if stripped and len(stripped) <= _RUNNING_HEAD_MAX_LEN:
            counts[stripped] = counts.get(stripped, 0) + 1

    repeated = {
        line for line, count in counts.items() if count >= _RUNNING_HEAD_MIN_REPEATS
    }
    if not repeated:
        return text

    kept = [
        line
        for line in lines
        # Голые номера страниц выкидываем всегда: они дают ложные совпадения
        # длиной в одно «слово», а смысла не несут.
        if line.strip() not in repeated and not line.strip().isdigit()
    ]
    return "\n".join(kept)


def pdf_to_checkable_text(pdf_bytes: bytes) -> str:
    """PDF → текст, готовый к сравнению: без вёрстки, колонтитулов и библиографии."""
    return strip_references(strip_running_heads(clean_text(extract_text(pdf_bytes))))

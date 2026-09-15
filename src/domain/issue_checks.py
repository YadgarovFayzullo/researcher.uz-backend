"""Алгоритмические проверки выпуска — то, что считается без модели.

Работают всегда, в том числе когда ANTHROPIC_API_KEY не задан: это обычный
детерминированный контроль, а не ИИ. Ловят они другой класс ошибок, чем модель,
и потому дополняют её, а не дублируют:

* **Пересечение страниц.** Две статьи выпуска не могут занимать одни и те же
  страницы. Модель видит по одной статье за раз и такого не заметит в принципе,
  а именно этот признак выдаёт «страницы 1-N» — когда редактор вписывает объём
  файла вместо пагинации номера, диапазоны накладываются у всех статей сразу.
* **Дубли.** Одинаковый DOI, одинаковый заголовок, один и тот же файл у двух
  записей — внутри выпуска и по всей платформе. Это тоже вопрос к базе, а не к
  тексту статьи.
* **Формат метаданных.** DOI не по стандарту, пустые обязательные поля,
  неразбираемые страницы. Тратить на это токены незачем.
* **Грубая сверка с PDF.** Слова заголовка и фамилии авторов должны
  встречаться в тексте файла; DOI и ISSN, найденные в PDF, должны совпадать с
  теми, что в базе. Это дешёвая нижняя граница: то, что ловится подстрокой,
  ловим здесь, а разбор «другая статья или иначе оформленная та же» оставляем
  модели.

Про строгость. Найденное здесь тоже гасит выпуск, поэтому major-порог
намеренно консервативен: сомнительное помечается как minor и просто показывается
владельцу. Ложная тревога стоит получаса разбирательства, но привычка к ложным
тревогам стоит того, что настоящую перестанут читать.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.demo import article_is_not_demo
from src.infrastructure.persistence.models import Article, Issue, Journal

MAJOR = "major"
MINOR = "minor"


@dataclass(frozen=True)
class Problem:
    field: str
    severity: str
    detail: str
    source: str = "rules"

    def as_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "severity": self.severity,
            "detail": self.detail,
            "source": self.source,
        }


# --------------------------------------------------------------------------
# Разбор полей
# --------------------------------------------------------------------------

# «12-20», «12–20» (en dash), «12 – 20», «12—20», а также «12-20, 25-30».
_RANGE = re.compile(r"(\d{1,5})\s*[-–—]\s*(\d{1,5})")
_SINGLE = re.compile(r"^\s*(\d{1,5})\s*$")

# DOI по стандарту Crossref: префикс 10.NNNN и произвольный суффикс.
_DOI = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
# ISSN печатается и как 1234-5678, и как «ISSN: 1234-5678».
_ISSN = re.compile(r"\b\d{4}[-–]\d{3}[\dXx]\b")

_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def page_range(pages: str | None) -> tuple[int, int] | None:
    """Первая и последняя страница. None — строка не разбирается.

    У статьи, разорванной по номеру («12-20, 25-30»), берём внешние границы:
    для поиска пересечений это правильная, пусть и грубая, оценка.
    """
    if not pages:
        return None
    matches = _RANGE.findall(pages)
    if matches:
        starts = [int(a) for a, _ in matches]
        ends = [int(b) for _, b in matches]
        low, high = min(starts), max(ends)
        return (low, high) if low <= high else (high, low)
    single = _SINGLE.match(pages)
    if single:
        value = int(single.group(1))
        return value, value
    return None


def normalize(text: str | None) -> str:
    """Строка к сравнимому виду: без регистра, пунктуации и лишних пробелов."""
    if not text:
        return ""
    return _SPACES.sub(" ", _NON_WORD.sub(" ", text.lower())).strip()


def words(text: str | None, *, min_length: int = 4) -> list[str]:
    return [w for w in normalize(text).split() if len(w) >= min_length]


def find_dois(text: str) -> set[str]:
    # Хвостовая пунктуация («doi.org/10.1234/abc.») прилипает к суффиксу и
    # ломает сравнение — срезаем.
    return {m.group(0).rstrip(".,);").lower() for m in _DOI.finditer(text)}


def find_issns(text: str) -> set[str]:
    return {m.group(0).replace("–", "-").upper() for m in _ISSN.finditer(text)}


def author_surnames(authors: str | None) -> list[str]:
    """Фамилии из строки авторов.

    Формат в базе разнобойный: и JSON-массив, и «Иванов И.И., Петров П.П.», и
    «I.I. Ivanov». Инициалы отсеиваем по длине — для поиска в тексте PDF нужны
    именно фамилии.
    """
    if not authors:
        return []
    raw = authors.strip("[]")
    parts = re.split(r"[,;\"']+", raw)
    surnames: list[str] = []
    for part in parts:
        for token in normalize(part).split():
            if len(token) >= 4 and not token.isdigit():
                surnames.append(token)
    return surnames


# --------------------------------------------------------------------------
# Проверки одной статьи: формат метаданных
# --------------------------------------------------------------------------


def check_fields(article: Article) -> list[Problem]:
    problems: list[Problem] = []
    if not (article.title or "").strip():
        problems.append(Problem("title", MAJOR, "у статьи нет заголовка"))
    if not (article.authors or "").strip():
        problems.append(Problem("authors", MAJOR, "не указаны авторы"))
    if not article.pdf:
        problems.append(Problem("other", MAJOR, "не приложен PDF"))

    if article.pages and page_range(article.pages) is None:
        problems.append(
            Problem(
                "pages",
                MINOR,
                f"страницы «{article.pages}» не разбираются как диапазон",
            )
        )
    elif not article.pages:
        problems.append(Problem("pages", MINOR, "не указаны страницы в выпуске"))

    doi = (article.doi or "").strip()
    if doi:
        cleaned = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
        if not _DOI.fullmatch(cleaned):
            problems.append(
                Problem("doi", MAJOR, f"DOI «{doi}» не соответствует формату 10.xxxx/...")
            )
    return problems


# --------------------------------------------------------------------------
# Проверки одной статьи против её PDF
# --------------------------------------------------------------------------

# Ниже этой доли слов заголовка, найденных в тексте, считаем, что в файле
# другая статья. Порог низкий намеренно: извлечение теряет буквы, склеивает
# слова и путает порядок строк.
TITLE_MAJOR_RATIO = 0.3
TITLE_MINOR_RATIO = 0.6
# Насколько объём файла может расходиться с диапазоном страниц, прежде чем это
# станет замечанием: титул, вставленный редакцией, и пустая страница в конце —
# обычное дело.
PAGES_TOLERANCE = 2


def check_against_pdf(
    article: Article,
    *,
    pdf_text: str,
    pdf_pages: int,
    journal: Journal | None,
) -> list[Problem]:
    """Сверка метаданных с текстом файла без модели — по вхождениям."""
    problems: list[Problem] = []
    haystack = normalize(pdf_text)
    if not haystack:
        return problems

    title_words = words(article.title)
    if title_words:
        hits = sum(1 for w in title_words if w in haystack)
        ratio = hits / len(title_words)
        if ratio < TITLE_MAJOR_RATIO:
            problems.append(
                Problem(
                    "title",
                    MAJOR,
                    f"заголовок из формы почти не встречается в PDF "
                    f"(совпало слов: {hits} из {len(title_words)})",
                )
            )
        elif ratio < TITLE_MINOR_RATIO:
            problems.append(
                Problem(
                    "title",
                    MINOR,
                    f"заголовок совпадает с PDF частично "
                    f"({hits} из {len(title_words)} слов)",
                )
            )

    surnames = author_surnames(article.authors)
    if surnames and not any(s in haystack for s in surnames):
        problems.append(
            Problem("authors", MAJOR, "ни одна фамилия из формы не найдена в PDF")
        )

    form_doi = (article.doi or "").strip().lower()
    form_doi = form_doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    pdf_dois = find_dois(pdf_text)
    if form_doi and pdf_dois and form_doi not in pdf_dois:
        problems.append(
            Problem(
                "doi",
                MAJOR,
                f"в PDF стоит другой DOI: {', '.join(sorted(pdf_dois)[:3])}, "
                f"в форме {form_doi}",
            )
        )

    journal_issns = {
        (value or "").replace("–", "-").upper()
        for value in ((journal.issn, journal.printed_issn) if journal else ())
        if value
    }
    pdf_issns = find_issns(pdf_text)
    if journal_issns and pdf_issns and not (pdf_issns & journal_issns):
        problems.append(
            Problem(
                "journal",
                MAJOR,
                f"в PDF указан чужой ISSN: {', '.join(sorted(pdf_issns)[:3])} "
                f"(у журнала {', '.join(sorted(journal_issns))})",
            )
        )

    span = page_range(article.pages)
    if span and pdf_pages:
        declared = span[1] - span[0] + 1
        if span[0] == 1 and span[1] == pdf_pages:
            problems.append(
                Problem(
                    "pages",
                    MAJOR,
                    f"страницы «{article.pages}» — это объём файла "
                    f"({pdf_pages} стр.), а не пагинация выпуска",
                )
            )
        elif abs(declared - pdf_pages) > PAGES_TOLERANCE:
            problems.append(
                Problem(
                    "pages",
                    MINOR,
                    f"в форме {declared} стр., в файле {pdf_pages}",
                )
            )
    return problems


# --------------------------------------------------------------------------
# Проверки по выпуску целиком
# --------------------------------------------------------------------------


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def describe_article(title: str | None) -> str:
    """Как называть статью в тексте замечания.

    Только заголовок: внутренний id владельцу ничего не говорит, а нужную
    строку он находит по названию — в уведомлении оно к тому же ссылка.
    """
    name = (title or "").strip()
    if len(name) > 70:
        name = name[:67].rstrip() + "…"
    return f"«{name}»" if name else "статья без названия"


# Доля статей номера, после которой замечание перестаёт считаться аномалией.
# Проверка на живых данных платформы: в некоторых журналах диапазоны страниц
# пересекаются у 250 статей из 262 — номер склеен из частей со своей нумерацией,
# и это особенность издания, а не подлог. Гасить за это выпуск нельзя: правило
# должно ловить статью, выбивающуюся из номера, а не сам номер.
EPIDEMIC_RATIO = 0.2
# Ниже этого числа статей доля ничего не значит: в номере из трёх статей две
# пересекающиеся — это 66%, но всё ещё аномалия.
EPIDEMIC_MIN_ARTICLES = 10


def check_issue(articles: Iterable[Article]) -> dict[int, list[Problem]]:
    """Взаимные противоречия внутри выпуска.

    Сравниваем ВСЕ статьи выпуска, а не только новые: чужая статья, залитая
    сегодня, накладывается на страницы той, что лежит здесь с прошлого года, и
    заметно это только в паре.
    """
    found: dict[int, list[Problem]] = defaultdict(list)
    rows = list(articles)

    ranges: list[tuple[Article, tuple[int, int]]] = [
        (a, span) for a in rows if (span := page_range(a.pages))
    ]
    for index, (article, span) in enumerate(ranges):
        for other, other_span in ranges[index + 1 :]:
            if not _overlap(span, other_span):
                continue
            # Совпадение диапазона один в один — это заявка двух статей на одни
            # и те же страницы, то есть почти наверняка дубль. Частичное
            # перекрытие бывает и от кривой пагинации, поэтому оно мягче.
            exact = span == other_span
            severity = MAJOR if exact else MINOR
            verb = "стоят ровно те же страницы, что и у" if exact else "пересекаются со страницами"
            found[article.id].append(
                Problem(
                    "pages",
                    severity,
                    f"с. {article.pages}: {verb} "
                    f"{describe_article(other.title)} (с. {other.pages})",
                )
            )
            found[other.id].append(
                Problem(
                    "pages",
                    severity,
                    f"с. {other.pages}: {verb} "
                    f"{describe_article(article.title)} (с. {article.pages})",
                )
            )

    _collect_duplicates(rows, found, scope="в этом же выпуске")
    return _demote_epidemics(found, total=len(rows))


def _demote_epidemics(
    found: dict[int, list[Problem]], *, total: int
) -> dict[int, list[Problem]]:
    """Понизить major до minor там, где замечание есть у большинства номера.

    Смысл: если «нарушают» почти все, нарушения нет — есть особенность
    издания. Замечание при этом не исчезает, его видно в админке, но выпуск
    из-за него не гаснет.
    """
    if total < EPIDEMIC_MIN_ARTICLES:
        return found

    affected: dict[str, set[int]] = defaultdict(set)
    for article_id, problems in found.items():
        for problem in problems:
            if problem.severity == MAJOR:
                affected[problem.field].add(article_id)

    epidemic = {
        field
        for field, ids in affected.items()
        if len(ids) / total > EPIDEMIC_RATIO
    }
    if not epidemic:
        return found

    return {
        article_id: [
            Problem(
                p.field,
                MINOR,
                f"{p.detail} — так же оформлено большинство статей номера, "
                f"это похоже на особенность журнала",
                p.source,
            )
            if p.field in epidemic and p.severity == MAJOR
            else p
            for p in problems
        ]
        for article_id, problems in found.items()
    }


def _collect_duplicates(
    rows: list[Article],
    found: dict[int, list[Problem]],
    *,
    scope: str,
) -> None:
    by_doi: dict[str, list[Article]] = defaultdict(list)
    by_title: dict[str, list[Article]] = defaultdict(list)
    by_pdf: dict[str, list[Article]] = defaultdict(list)
    for article in rows:
        if article.doi:
            by_doi[article.doi.strip().lower()].append(article)
        title = normalize(article.title)
        if len(title) > 20:
            by_title[title].append(article)
        if article.pdf:
            by_pdf[article.pdf.strip()].append(article)

    def _others(group: list[Article], current: Article) -> str:
        return ", ".join(
            describe_article(a.title) for a in group if a.id != current.id
        )

    for key, group in by_doi.items():
        if len(group) > 1:
            for article in group:
                found[article.id].append(
                    Problem(
                        "doi",
                        MAJOR,
                        f"тот же DOI {key} стоит {scope} у {_others(group, article)}",
                    )
                )
    for group in by_title.values():
        if len(group) > 1:
            for article in group:
                found[article.id].append(
                    Problem(
                        "title",
                        MAJOR,
                        f"такой же заголовок {scope} у {_others(group, article)}",
                    )
                )
    for group in by_pdf.values():
        if len(group) > 1:
            for article in group:
                found[article.id].append(
                    Problem(
                        "other",
                        MAJOR,
                        f"тот же самый файл приложен {scope} к {_others(group, article)}",
                    )
                )


async def check_platform_duplicates(
    db: AsyncSession, articles: Iterable[Article]
) -> dict[int, list[Problem]]:
    """Тот же DOI, заголовок или файл — но уже где-то ещё на платформе.

    Именно так выглядела история, из-за которой всё это написано: статьи,
    опубликованные в этом же журнале в 2023-м, завели повторно в свежий номер
    с новыми DOI. Внутри выпуска дубля нет, он виден только по базе.
    """
    found: dict[int, list[Problem]] = defaultdict(list)
    rows = list(articles)
    if not rows:
        return found

    ids = {a.id for a in rows}
    dois = {a.doi.strip().lower() for a in rows if a.doi}
    titles = {normalize(a.title) for a in rows if len(normalize(a.title)) > 20}
    pdfs = {a.pdf.strip() for a in rows if a.pdf}

    if dois:
        clashes = (
            await db.execute(
                select(Article.id, Article.doi, Article.title, Article.issue_id).where(
                    Article.id.notin_(ids),
                    # Демо-статьи (показ клиентам, копии для демо-профиля) — не
                    # «уже опубликованное»: копия с тем же заголовком иначе
                    # погасила бы выпуск настоящего журнала за дубль.
                    article_is_not_demo(),
                    func.lower(Article.doi).in_(dois),
                )
            )
        ).all()
        by_doi = {
            (doi or "").strip().lower(): (aid, title, iid)
            for aid, doi, title, iid in clashes
        }
        for article in rows:
            key = (article.doi or "").strip().lower()
            if key and key in by_doi:
                other_id, other_title, other_issue = by_doi[key]
                found[article.id].append(
                    Problem(
                        "doi",
                        MAJOR,
                        f"этот DOI уже стоит на платформе у "
                        f"{describe_article(other_title)}",
                    )
                )

    if titles:
        # Заголовок сравниваем нормализованным (без регистра и пунктуации), а
        # индекса по такому виду в базе нет. Тянуть весь каталог ради этого
        # незачем: сначала отбираем кандидатов по началу заголовка через LIKE
        # (btree его использует), а нормализованное сравнение делаем уже по
        # этой горстке строк.
        prefixes = [(a.title or "")[:40] for a in rows if (a.title or "").strip()]
        # `%` и `_` в заголовке («рост на 50%») — это подстановки LIKE, их надо
        # экранировать, иначе запрос вернёт мусор.
        patterns = [
            p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for p in prefixes[:50]
        ]
        candidates = (
            await db.execute(
                select(Article.id, Article.title, Article.issue_id).where(
                    Article.id.notin_(ids),
                    article_is_not_demo(),
                    or_(*[Article.title.ilike(p, escape="\\") for p in patterns]),
                )
            )
        ).all() if patterns else []
        by_title: dict[str, tuple[int, str | None, int | None]] = {}
        for other_id, other_title, other_issue in candidates:
            by_title.setdefault(normalize(other_title), (other_id, other_title, other_issue))
        for article in rows:
            key = normalize(article.title)
            if key and key in by_title:
                other_id, other_title, other_issue = by_title[key]
                found[article.id].append(
                    Problem(
                        "title",
                        MAJOR,
                        f"статья с таким же заголовком уже опубликована на "
                        f"платформе: {describe_article(other_title)}",
                    )
                )

    if pdfs:
        clashes = (
            await db.execute(
                select(Article.id, Article.pdf, Article.title).where(
                    Article.id.notin_(ids), article_is_not_demo(), Article.pdf.in_(pdfs)
                )
            )
        ).all()
        by_pdf = {(pdf or "").strip(): (aid, title) for aid, pdf, title in clashes}
        for article in rows:
            key = (article.pdf or "").strip()
            if key and key in by_pdf:
                found[article.id].append(
                    Problem(
                        "other",
                        MAJOR,
                        f"этот же файл приложен к статье ID {by_pdf[key]}",
                    )
                )
    return found


async def collect(
    db: AsyncSession,
    *,
    issue: Issue,
    journal: Journal | None,
    queue: list[Article],
    pdf_info: dict[int, tuple[str, int]],
) -> dict[int, list[Problem]]:
    """Все алгоритмические замечания по статьям очереди.

    `pdf_info` — уже извлечённые текст и число страниц по id статьи; читать
    файлы второй раз ради правил незачем, их скачивает вызывающий.
    """
    all_articles = (
        await db.execute(select(Article).where(Article.issue_id == issue.id))
    ).scalars().all()

    result: dict[int, list[Problem]] = defaultdict(list)
    issue_problems = check_issue(all_articles)
    platform_problems = await check_platform_duplicates(db, queue)

    for article in queue:
        problems = list(check_fields(article))
        text, pages = pdf_info.get(article.id, ("", 0))
        if text:
            problems.extend(
                check_against_pdf(
                    article, pdf_text=text, pdf_pages=pages, journal=journal
                )
            )
        problems.extend(issue_problems.get(article.id, []))
        problems.extend(platform_problems.get(article.id, []))
        if problems:
            result[article.id] = problems
    return result

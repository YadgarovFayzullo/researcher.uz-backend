"""Автопроверка выпуска перед публикацией.

Зачем. История, из-за которой написана `moderation.py`, повторяема: редактор
журнала завёл в свой номер чужие статьи — PDF с обложками других изданий,
страницы «1-N» (число страниц файла вместо пагинации выпуска), новые DOI на
уже опубликованный текст. Нашли это руками и постфактум, а санкция Google
Scholar коллективная: снимают площадку целиком. Значит проверять надо ДО того,
как статья попала в ленты, sitemap и OAI, — то есть до публикации.

Как это устроено:

1. Статья, заведённая через форму админки, создаётся ЧЕРНОВИКОМ и получает
   отметку `articles.metadata.ai_review = {"status": "queued"}`. Отметка нужна,
   чтобы отличить «залито сейчас и ждёт проверки» от десятков тысяч черновиков,
   приехавших импортом архивов: у импорта свой воркфлоу, и поднимать его статьи
   этот код не должен.
2. Планировщик (`src/infrastructure/scheduler.py`) ждёт час после ПОСЛЕДНЕЙ
   залитой в выпуск статьи и проверяет весь накопившийся хвост разом. Час — не
   осторожность, а необходимость: номер заливают порциями по 10-20 статей, и
   проверка после каждой означала бы проверку недособранного выпуска.
3. Проверка идёт двумя слоями, и они ловят разное:
   * **Правила** (`src/domain/issue_checks.py`) — то, что считается точно и
     бесплатно: пересечение диапазонов страниц между статьями выпуска, дубли
     DOI/заголовков/файлов внутри выпуска и по всей платформе, формат
     метаданных, вхождение заголовка и фамилий в текст PDF. Часть этого модель
     не увидела бы в принципе: она смотрит на одну статью, а пересечение
     страниц — свойство пары.
   * **Модель** — сверка «описывают ли метаданные именно этот файл» там, где
     regex бессилен: у одного журнала ФИО идут после заголовка, у другого
     сначала кафедра, а экстрактор отдаёт текст не в визуальном порядке.
   Каким слоям работать, задаёт `ISSUE_REVIEW_MODE` (rules | llm | both).
   Чья модель отвечает — `LLM_PROVIDER` (gemini | openrouter | anthropic,
   см. `src/infrastructure/external/llm.py`). Без ключа модели остаются одни
   правила — фича работает и так.
4. Все чисто → статьи публикуются молча. Есть серьёзное расхождение → выпуск
   гасится тем же `moderation.block_issue`, что и ручное снятие, а владельцу
   уходит разбор в Telegram с двумя кнопками. Решение всегда за человеком:
   проверка только сортирует «можно не смотреть» и «посмотрите обязательно».

Чего этот код НЕ делает. Не ищет заимствования (для этого есть антиплагиат по
базе платформы) и не выносит приговор о научном качестве. Он отвечает ровно на
один вопрос: описывают ли метаданные, введённые редактором, тот файл, который
он приложил, и не противоречат ли статьи выпуска друг другу.

Важное свойство: непроверенное — не то же самое, что чистое. Скан без
текстового слоя и отсутствующий файл всегда оставляют статью черновиком, а
владельцу уходит уведомление. Отказ самой модели (кончились кредиты, лежит
API) по умолчанию не блокирует публикацию, если слой правил отработал и молчит,
— иначе проблемы с чужим биллингом превращались бы в ручную работу; строгое
поведение включается флагом AI_REVIEW_REQUIRED.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.domain import issue_checks
from src.domain.moderation import (
    block_issue,
    issue_is_blocked,
    set_meta,
    unblock_issue,
)
from src.infrastructure.external import llm, telegram
from src.infrastructure.pdf_text import clean_text, extract_text, page_count
from src.infrastructure.persistence.models import Article, Issue, Journal
from src.infrastructure.storage import StorageNotConfigured, key_from_url, storage

logger = logging.getLogger(__name__)

# Статусы `articles.metadata.ai_review.status`.
QUEUED = "queued"        # залито через форму, ждёт проверки
PASSED = "passed"        # проверено, расхождений нет → опубликовано
FLAGGED = "flagged"      # расхождения, из-за которых погашен выпуск
UNCHECKED = "unchecked"  # проверить не удалось (скан, нет PDF, сбой API)

# Сколько символов текста PDF отдаём модели. Три страницы вёрстки — это
# примерно 6-8 тысяч знаков; потолок отсекает патологию вроде PDF, где весь
# текст лежит одной строкой.
MAX_PDF_CHARS = 20000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso() -> str:
    return _now().isoformat()


# --------------------------------------------------------------------------
# Постановка в очередь
# --------------------------------------------------------------------------


def queue_article(article: Article) -> None:
    """Пометить статью как ждущую проверки (без commit — его делает вызывающий).

    Ставится только на статьи в выпуске: у отдельных изданий (монографии,
    диссертации) нет выпуска, который можно погасить, и вся эта механика к ним
    неприменима.
    """
    if not article.issue_id or not settings.REVIEW_ACTIVE:
        return
    set_meta(article, "ai_review", {"status": QUEUED, "at": _iso(), "attempts": 0})


# --------------------------------------------------------------------------
# Выбор созревших выпусков
# --------------------------------------------------------------------------


async def due_issue_ids(db: AsyncSession) -> list[int]:
    """Выпуски, где очередь непроверенных статей отлежала положенную паузу.

    Пауза считается от самой свежей статьи в очереди: пока редактор льёт номер,
    таймер каждый раз сдвигается, и проверка запускается один раз по готовому
    выпуску, а не после каждой порции.
    """
    threshold = _now() - timedelta(minutes=settings.AI_REVIEW_DELAY_MINUTES)
    rows = (
        await db.execute(
            select(Article.issue_id, func.max(Article.created_at))
            .where(
                Article.issue_id.isnot(None),
                Article.published.is_(False),
                Article.meta["ai_review"]["status"].astext == QUEUED,
            )
            .group_by(Article.issue_id)
            .having(func.max(Article.created_at) < threshold)
        )
    ).all()
    return [int(issue_id) for issue_id, _ in rows]


# --------------------------------------------------------------------------
# Материал для модели
# --------------------------------------------------------------------------


async def _pdf_text(article: Article) -> tuple[str, int, str | None]:
    """Текст первых страниц PDF статьи, число страниц и причина отказа.

    Колонтитулы здесь НЕ вычищаются (в отличие от антиплагиата): нам нужен
    именно колонтитул — в нём стоит название чужого журнала, если статья
    перепечатана.
    """
    if not article.pdf:
        return "", 0, "к статье не приложен PDF"

    key = key_from_url(article.pdf, default_prefix="pdfs")
    try:
        body, _ = await asyncio.to_thread(storage.get, key)
    except StorageNotConfigured:
        return "", 0, "хранилище файлов не настроено"
    except Exception:
        logger.exception("ai_review: не удалось скачать PDF статьи %s", article.id)
        return "", 0, "PDF не скачался из хранилища"

    pages = await asyncio.to_thread(page_count, body)
    raw = await asyncio.to_thread(
        extract_text, body, max_pages=settings.AI_REVIEW_PDF_PAGES
    )
    text = clean_text(raw)[:MAX_PDF_CHARS]
    if not text.strip():
        # Скан без текстового слоя. Это не нарушение и не повод гасить выпуск —
        # просто проверять нечем, и сказать об этом надо прямо.
        return "", pages, "PDF без текстового слоя (скан)"
    return text, pages, None


def _describe(article: Article, issue: Issue, journal: Journal | None) -> str:
    """Метаданные статьи так, как их ввёл редактор."""
    lines = [
        f"Журнал (по базе): {journal.name if journal else '—'}",
        f"ISSN (по базе): {journal.issn or journal.printed_issn or '—'}"
        if journal
        else "ISSN (по базе): —",
        f"Выпуск: год {issue.year or '—'}, том {issue.volume or '—'}, "
        f"номер {issue.issue or '—'}",
        "",
        f"Заголовок: {article.title or '—'}",
        f"Заголовок (второй язык): {article.title_foreign or '—'}",
        f"Авторы: {article.authors or '—'}",
        f"Страницы в выпуске: {article.pages or '—'}",
        f"DOI: {article.doi or '—'}",
        f"Ключевые слова: {article.keywords or '—'}",
        f"Аннотация: {(article.annotation or '—')[:1500]}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Промпт и схема ответа
# --------------------------------------------------------------------------

SYSTEM = """\
Ты — редакционный контролёр научной издательской платформы researcher.uz.

Тебе дают метаданные статьи, которые редактор журнала ввёл руками, и текст
первых страниц PDF-файла, который он к ней приложил. Твоя задача — ответить на
один вопрос: описывают ли эти метаданные именно этот файл.

Что считать СЕРЬЁЗНЫМ расхождением (severity = "major"):
- заголовок в PDF по смыслу другой, а не иначе оформленный;
- в PDF другие авторы (не переставленные и не иначе транслитерированные, а
  другие люди) либо авторов в форме нет вовсе;
- в колонтитуле, шапке или подвале PDF стоит НЕ тот журнал, что в базе, либо
  чужой ISSN — признак перепечатки из другого издания;
- DOI в PDF не совпадает с DOI в форме (разные суффиксы одного префикса — тоже
  расхождение);
- страницы в форме заведомо не пагинация выпуска: диапазон начинается с 1 и
  совпадает с числом страниц файла, тогда как в PDF видна другая нумерация;
- файл вообще не научная статья (обложка выпуска, содержание, титульный лист,
  справка, пустой шаблон).

Что считать МЕЛОЧЬЮ (severity = "minor") и НЕ поводом останавливать выпуск:
- разная транслитерация имён, инициалы против полного имени, другой порядок
  авторов;
- различия в регистре, пунктуации, переносах, кавычках;
- аннотация или ключевые слова в форме короче/длиннее, чем в PDF, либо на
  другом языке из тех, что есть в файле;
- страницы в форме не видны в тексте PDF (колонтитул не попал в извлечение);
- опечатки, не меняющие смысл.

Правила вывода:
- verdict = "suspect", только если есть хотя бы одно major-расхождение;
- сомневаешься между minor и major — ставь minor: цена ошибки несимметрична,
  ложная тревога стоит владельцу платформы получаса разбирательства, а
  пропущенная перепечатка — снятия площадки из Google Scholar;
- текст PDF извлечён автоматически, порядок строк может быть нарушен, буквы
  склеены, формулы потеряны — не принимай дефекты извлечения за расхождения;
- в detail пиши по-русски, коротко и конкретно: что в форме, что в файле.\
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "suspect"]},
        "summary": {
            "type": "string",
            "description": "Одна фраза по-русски: что не так или что всё сходится",
        },
        "pdf_journal": {
            "type": ["string", "null"],
            "description": "Название журнала, найденное в самом PDF, или null",
        },
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "enum": [
                            "title",
                            "authors",
                            "pages",
                            "doi",
                            "journal",
                            "annotation",
                            "keywords",
                            "document_type",
                            "other",
                        ],
                    },
                    "severity": {"type": "string", "enum": ["minor", "major"]},
                    "detail": {"type": "string"},
                },
                "required": ["field", "severity", "detail"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "summary", "pdf_journal", "problems"],
    "additionalProperties": False,
}


async def ask_model(
    article: Article,
    issue: Issue,
    journal: Journal | None,
    *,
    text: str,
    pages: int,
) -> dict[str, Any]:
    """Спросить модель по одной статье. Текст PDF уже извлечён вызывающим.

    Сбой пробрасывается исключением: вызывающий обязан отличить «проверено,
    чисто» от «не проверено», иначе недоступность API молча опубликовала бы
    непроверенный номер.
    """
    user = (
        f"МЕТАДАННЫЕ ИЗ ФОРМЫ:\n{_describe(article, issue, journal)}\n\n"
        f"PDF: {pages} стр. в файле; ниже текст первых "
        f"{min(pages, settings.AI_REVIEW_PDF_PAGES)} стр.\n"
        f"---\n{text}\n---"
    )
    result = await llm.structured(system=SYSTEM, user=user, schema=SCHEMA)

    problems = [
        {**p, "source": "llm"}
        for p in result.get("problems") or []
        if isinstance(p, dict)
    ]
    return {
        "model": llm.active_model(),
        "verdict": result.get("verdict"),
        "summary": result.get("summary"),
        "pdf_journal": result.get("pdf_journal"),
        "problems": problems,
    }


# --------------------------------------------------------------------------
# Прогон выпуска
# --------------------------------------------------------------------------


async def review_issue(db: AsyncSession, issue_id: int) -> dict[str, Any]:
    """Проверить накопившуюся очередь выпуска и применить решение."""
    issue = (
        await db.execute(select(Issue).where(Issue.id == issue_id))
    ).scalars().first()
    if issue is None:
        return {"status": "skipped", "reason": "выпуск не найден"}
    if issue_is_blocked(issue):
        # Выпуск уже погашен — дальше решает владелец, а не мы.
        return {"status": "skipped", "reason": "выпуск заблокирован"}

    journal = (
        (
            await db.execute(select(Journal).where(Journal.id == issue.journal_id))
        ).scalars().first()
        if issue.journal_id
        else None
    )

    queue = (
        await db.execute(
            select(Article).where(
                Article.issue_id == issue_id,
                Article.published.is_(False),
                Article.meta["ai_review"]["status"].astext == QUEUED,
            )
        )
    ).scalars().all()
    if not queue:
        return {"status": "skipped", "reason": "очередь пуста"}

    # 1. Один раз скачиваем и разбираем файлы: текст нужен обоим слоям.
    semaphore = asyncio.Semaphore(max(1, settings.AI_REVIEW_CONCURRENCY))

    async def fetch(article: Article) -> tuple[int, tuple[str, int, str | None]]:
        async with semaphore:
            return article.id, await _pdf_text(article)

    pdf_raw = dict(await asyncio.gather(*(fetch(a) for a in queue)))
    pdf_info = {aid: (text, pages) for aid, (text, pages, _) in pdf_raw.items()}
    skip_reasons = {aid: reason for aid, (_, _, reason) in pdf_raw.items() if reason}

    # 2. Алгоритмический слой: страницы, дубли, формат, вхождения в текст.
    rule_problems: dict[int, list[dict[str, Any]]] = {}
    if settings.review_uses_rules:
        found = await issue_checks.collect(
            db, issue=issue, journal=journal, queue=list(queue), pdf_info=pdf_info
        )
        rule_problems = {
            aid: [p.as_dict() for p in problems] for aid, problems in found.items()
        }

    # 3. Слой модели — только там, где есть что читать.
    async def ask(article: Article) -> tuple[int, dict[str, Any] | None | str]:
        if not settings.review_uses_llm or not pdf_info.get(article.id, ("", 0))[0]:
            return article.id, None
        text, pages = pdf_info[article.id]
        async with semaphore:
            try:
                return article.id, await ask_model(
                    article, issue, journal, text=text, pages=pages
                )
            except Exception:
                logger.exception("ai_review: модель не ответила по статье %s", article.id)
                # Строка-маркер, а не None: «модель не спрашивали» и «модель не
                # ответила» ведут к разным решениям.
                return article.id, "error"

    model_results = dict(await asyncio.gather(*(ask(a) for a in queue)))

    # 4. Сводим оба слоя в один вердикт по каждой статье.
    passed: list[Article] = []
    flagged: list[tuple[Article, dict[str, Any]]] = []
    unchecked: list[tuple[Article, dict[str, Any]]] = []
    retry: list[Article] = []

    for article in queue:
        model = model_results.get(article.id)
        model_failed = model == "error"
        model_data: dict[str, Any] = model if isinstance(model, dict) else {}

        problems = [
            *rule_problems.get(article.id, []),
            *(model_data.get("problems") or []),
        ]
        major = [p for p in problems if p.get("severity") == "major"]

        if model_failed and not major and settings.AI_REVIEW_REQUIRED:
            # Модель не ответила, а правила молчат — публиковать нельзя:
            # непроверенное не равно чистому. Возвращаем в очередь до
            # исчерпания попыток.
            previous = (article.meta or {}).get("ai_review") or {}
            attempts = int(previous.get("attempts") or 0) + 1
            if attempts < settings.AI_REVIEW_MAX_ATTEMPTS:
                set_meta(
                    article,
                    "ai_review",
                    {**previous, "status": QUEUED, "attempts": attempts},
                )
                retry.append(article)
                continue
            record = {
                "status": UNCHECKED,
                "at": _iso(),
                "reason": "не удалось получить ответ модели",
                "attempts": attempts,
                "problems": problems,
            }
            set_meta(article, "ai_review", record)
            unchecked.append((article, record))
            continue

        skip_reason = skip_reasons.get(article.id)
        if major:
            status = FLAGGED
        elif skip_reason:
            # Файла нет или это скан: правила отработали по метаданным, но
            # сверить с содержимым нечем — под автопубликацию не подходит.
            status = UNCHECKED
        else:
            status = PASSED

        record = {
            "status": status,
            "at": _iso(),
            # Отметка остаётся в истории статьи: по ней видно, что вердикт
            # опирался на одни правила, даже если модель была включена.
            "llm_error": model_failed or None,
            "model": model_data.get("model") if model_data else None,
            "verdict": model_data.get("verdict"),
            "summary": model_data.get("summary")
            or (major[0]["detail"] if major else None),
            "pdf_journal": model_data.get("pdf_journal"),
            "reason": skip_reason,
            "problems": problems,
        }
        set_meta(article, "ai_review", record)
        if status == PASSED:
            passed.append(article)
        elif status == FLAGGED:
            flagged.append((article, record))
        else:
            unchecked.append((article, record))

    if retry and not (passed or flagged or unchecked):
        # Ничего не проверилось (скорее всего API лежит) — сохраняем счётчики
        # попыток и уходим до следующего тика, ничего не публикуя.
        await db.commit()
        return {"status": "retry", "issue_id": issue_id, "retry": len(retry)}

    run_id = uuid.uuid4().hex[:12]
    outcome = FLAGGED if flagged else PASSED

    if flagged:
        # Гасим номер целиком — тем же механизмом, что и ручное снятие: у
        # выпуска, куда завели чужую статью, доверия нет ко всему содержимому.
        # Проверенные «чистые» статьи этого прогона остаются черновиками: их
        # поднимет решение владельца.
        first_title = (flagged[0][0].title or "").strip()
        reason = (
            f"Автопроверка: расхождения в метаданных "
            f"({len(flagged)} из {len(queue)} статей, напр. «{first_title[:80]}»)"
        )
        await block_issue(
            db,
            issue,
            reason=reason,
            by=None,
            trigger_article_id=flagged[0][0].id,
        )
    else:
        for article in passed:
            article.published = True

    review = {
        "run_id": run_id,
        "at": _iso(),
        # Чем именно проверяли: «rules», «llm» или «rules+llm» — по нему видно,
        # был ли доступен ключ в момент прогона.
        "model": "+".join(
            part
            for part, on in (
                ("rules", settings.review_uses_rules),
                (llm.active_model(), settings.review_uses_llm),
            )
            if on
        ),
        "status": outcome,
        "checked": len(queue),
        # Сколько статей проверено без модели, потому что она не ответила.
        # Ноль при выключенном слое — там её и не спрашивали.
        "llm_errors": sum(1 for v in model_results.values() if v == "error"),
        "passed_ids": [a.id for a in passed],
        "flagged_ids": [a.id for a, _ in flagged],
        "unchecked_ids": [a.id for a, _ in unchecked],
        # Решение владельца, когда оно будет принято: publish | keep_blocked.
        "decision": None,
    }
    set_meta(issue, "ai_review", review)
    await db.commit()

    await _notify(
        issue=issue,
        journal=journal,
        review=review,
        flagged=flagged,
        unchecked=unchecked,
        passed_count=len(passed),
    )
    # Второй commit — за id отправленного сообщения: по нему кнопки гасятся,
    # когда решение принято не из Telegram, а в админке.
    await db.commit()
    return {"status": outcome, "issue_id": issue_id, **review}


async def audit_issue(
    db: AsyncSession, issue_id: int, *, max_articles: int = 60
) -> dict[str, Any]:
    """Проверить выпуск целиком и ТОЛЬКО отчитаться — ничего не меняя.

    Нужно для панели: там владелец выбирает любой выпуск, в том числе давно
    опубликованный, где очереди нет. Применять к архиву те же санкции нельзя —
    на живой базе правила находят расхождения в трети выпусков, и автоматическое
    гашение снесло бы полкаталога из-за ошибок многолетней давности. Поэтому
    здесь только чтение: что нашли, то и показали, решение — за человеком.
    """
    issue = (
        await db.execute(select(Issue).where(Issue.id == issue_id))
    ).scalars().first()
    if issue is None:
        return {"status": "error", "reason": "выпуск не найден"}

    journal = (
        (
            await db.execute(select(Journal).where(Journal.id == issue.journal_id))
        ).scalars().first()
        if issue.journal_id
        else None
    )
    rows = (
        await db.execute(select(Article).where(Article.issue_id == issue_id))
    ).scalars().all()
    queue = list(rows)[:max_articles]
    if not queue:
        return {"status": "empty", "issue_id": issue_id, "articles": []}

    semaphore = asyncio.Semaphore(max(1, settings.AI_REVIEW_CONCURRENCY))

    async def fetch(article: Article) -> tuple[int, tuple[str, int, str | None]]:
        async with semaphore:
            return article.id, await _pdf_text(article)

    raw = dict(await asyncio.gather(*(fetch(a) for a in queue)))
    pdf_info = {aid: (text, pages) for aid, (text, pages, _) in raw.items()}
    skip_reasons = {aid: reason for aid, (_, _, reason) in raw.items() if reason}

    found = await issue_checks.collect(
        db, issue=issue, journal=journal, queue=queue, pdf_info=pdf_info
    )

    articles: list[dict[str, Any]] = []
    for article in queue:
        problems = [p.as_dict() for p in found.get(article.id, [])]
        major = [p for p in problems if p["severity"] == "major"]
        if not problems and article.id not in skip_reasons:
            continue
        articles.append(
            {
                "id": article.id,
                "title": article.title,
                "pages": article.pages,
                "doi": article.doi,
                "published": bool(article.published),
                "status": FLAGGED if major else (UNCHECKED if article.id in skip_reasons else PASSED),
                "reason": skip_reasons.get(article.id),
                "problems": problems,
            }
        )

    return {
        "status": "ok",
        "issue_id": issue_id,
        "journal": journal.name if journal else None,
        "total": len(rows),
        "checked": len(queue),
        "flagged": sum(1 for a in articles if a["status"] == FLAGGED),
        "unchecked": sum(1 for a in articles if a["status"] == UNCHECKED),
        "articles": articles,
    }


# --------------------------------------------------------------------------
# Уведомление владельцу
# --------------------------------------------------------------------------


def _issue_label(issue: Issue) -> str:
    """Как назвать выпуск человеку: «2026, том 12, №1»."""
    parts = [
        p
        for p in (
            str(issue.year) if issue.year else "",
            f"том {issue.volume}" if issue.volume else "",
            f"№{issue.issue}" if issue.issue else "",
        )
        if p
    ]
    if not parts and issue.title:
        return issue.title
    return ", ".join(parts) or "без номера"


def _articles_url(issue: Issue) -> str:
    """Список статей выпуска — именно туда владелец идёт разбираться.

    Маршрут вложенный: экран живёт в
    `admin/journals/publisher/<journalId>/articles/<issueId>`.
    """
    base = settings.ADMIN_BASE_URL.rstrip("/")
    return (
        f"{base}/ru/admin/journals/publisher/{issue.journal_id}"
        f"/articles/{issue.id}"
    )


def _article_url(article: Article) -> str:
    base = settings.ADMIN_BASE_URL.rstrip("/")
    return f"{base}/ru/admin/articles/edit/{article.id}?issueId={article.issue_id}"


async def _notify(
    *,
    issue: Issue,
    journal: Journal | None,
    review: dict[str, Any],
    flagged: list[tuple[Article, dict[str, Any]]],
    unchecked: list[tuple[Article, dict[str, Any]]],
    passed_count: int,
) -> None:
    """Написать владельцу — но только когда есть о чём.

    Чистый выпуск публикуется молча: уведомление, на которое всегда отвечают
    «ок», через месяц перестают читать, и тогда не заметят важное.
    """
    if not (flagged or unchecked):
        return
    if not settings.TELEGRAM_ENABLED:
        logger.warning(
            "ai_review: выпуск %s требует внимания, но Telegram не настроен", issue.id
        )
        return

    e = telegram.escape
    journal_name = (journal.name if journal else None) or "журнал не указан"

    lines: list[str] = []
    lines.append(
        "⛔ <b>Выпуск снят автопроверкой</b>"
        if flagged
        else "⚠️ <b>Выпуск проверен не полностью</b>"
    )
    lines.append("")
    lines.append(f"📚 <b>Журнал:</b> {e(journal_name)}")
    lines.append(
        f"📗 <b>Выпуск:</b> <a href=\"{_articles_url(issue)}\">"
        f"{e(_issue_label(issue))}</a>"
    )
    lines.append(
        f"🔍 <b>Проверено:</b> {review['checked']} статей — "
        f"чисто {passed_count}, с расхождениями {len(flagged)}, "
        f"не проверено {len(unchecked)}"
    )

    # Название статьи — ссылкой на её форму в админке: из уведомления должно
    # быть видно, о чём речь, и одним касанием попадать туда, где это правится.
    if flagged:
        lines.append("")
        lines.append("<b>Расхождения</b>")
        for index, (article, verdict) in enumerate(flagged, start=1):
            title = (article.title or "без названия").strip()
            lines.append("")
            lines.append(
                f"{index}. <a href=\"{_article_url(article)}\">{e(title[:110])}</a>"
            )
            meta_bits = []
            if article.pages:
                meta_bits.append(f"с. {e(article.pages)}")
            if article.doi:
                meta_bits.append(f"DOI {e(article.doi)}")
            if meta_bits:
                lines.append(f"     <i>{' · '.join(meta_bits)}</i>")
            for problem in verdict.get("problems") or []:
                if problem.get("severity") != "major":
                    continue
                lines.append(f"     ⚠️ {e(problem.get('detail'))}")

    if unchecked:
        lines.append("")
        lines.append("<b>Не удалось проверить</b>")
        for article, verdict in unchecked:
            title = (article.title or "без названия").strip()
            lines.append(
                f"• <a href=\"{_article_url(article)}\">{e(title[:110])}</a> — "
                f"{e(verdict.get('reason') or 'причина не записана')}"
            )

    lines.append("")
    lines.append("<b>Что дальше</b>")
    if flagged:
        lines.append(
            "✅ <b>Опубликовать выпуск</b> — снять блокировку и опубликовать все "
            "статьи номера, включая спорные."
        )
        lines.append(
            "⛔ <b>Оставить закрытым</b> — выпуск остаётся снятым с сайта, "
            "метаданные правятся в админке."
        )
    else:
        lines.append(
            "Непроверенные статьи лежат черновиками. «Опубликовать выпуск» "
            "поднимет их как есть, «Оставить закрытым» — оставит черновиками."
        )

    token = f"{issue.id}:{review['run_id']}"
    buttons = [
        [
            {"text": "✅ Опубликовать выпуск", "callback_data": f"aipub:{token}"},
            {"text": "⛔ Оставить закрытым", "callback_data": f"aikeep:{token}"},
        ],
        [{"text": "📄 Статьи выпуска", "url": _articles_url(issue)}],
    ]
    message_id = await telegram.send_long("\n".join(lines), buttons=buttons)
    if message_id:
        review["telegram_message_id"] = message_id
        set_meta(issue, "ai_review", review)


# --------------------------------------------------------------------------
# Решение владельца (кнопки в Telegram и ручки админки)
# --------------------------------------------------------------------------


async def apply_decision(
    db: AsyncSession,
    issue_id: int,
    *,
    decision: str,
    run_id: str | None = None,
    by: str | None = None,
) -> dict[str, Any]:
    """Применить решение владельца: `publish` или `keep_blocked`.

    `run_id` защищает от нажатия кнопки под старым сообщением: за месяц в чате
    накопится десяток разборов, и «опубликовать» из прошлогоднего не должно
    поднимать сегодняшний выпуск.
    """
    issue = (
        await db.execute(select(Issue).where(Issue.id == issue_id))
    ).scalars().first()
    if issue is None:
        return {"status": "error", "reason": "выпуск не найден"}

    review = dict((issue.meta or {}).get("ai_review") or {})
    if not review:
        return {"status": "error", "reason": "по этому выпуску нет ИИ-проверки"}
    if run_id and review.get("run_id") != run_id:
        return {"status": "error", "reason": "проверка устарела, откройте админку"}
    if review.get("decision"):
        return {"status": "already", "decision": review["decision"]}

    restored = 0
    if decision == "publish":
        if issue_is_blocked(issue):
            restored = await unblock_issue(db, issue)
        ids = [
            *(review.get("flagged_ids") or []),
            *(review.get("passed_ids") or []),
            *(review.get("unchecked_ids") or []),
        ]
        if ids:
            rows = (
                await db.execute(select(Article).where(Article.id.in_(ids)))
            ).scalars().all()
            for article in rows:
                # Снятое владельцем за нарушение поднимать не даём даже отсюда —
                # takedown отменяется отдельно и осознанно.
                if (article.meta or {}).get("takedown"):
                    continue
                article.published = True
                restored += 1
    elif decision != "keep_blocked":
        return {"status": "error", "reason": f"неизвестное решение: {decision}"}

    review["decision"] = decision
    review["decided_at"] = _iso()
    review["decided_by"] = by
    set_meta(issue, "ai_review", review)
    await db.commit()
    return {
        "status": "ok",
        "decision": decision,
        "issue_id": issue_id,
        "published": restored,
    }

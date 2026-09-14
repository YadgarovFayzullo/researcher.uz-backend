"""Автопроверка выпуска: правила, вердикты и решение владельца.

Что стережём:
  * разбор полей (страницы, DOI, ISSN, фамилии) не разъезжается на реальных
    форматах, которые встречаются в базе;
  * пересечение страниц и дубли DOI/файла ловятся именно как major — это они
    гасят выпуск;
  * чистая очередь публикуется сама, грязная гасит выпуск и оставляет всё
    черновиками;
  * решение владельца `publish` поднимает выпуск целиком и повторно уже не
    срабатывает.

Модель здесь не вызывается: прогон идёт в режиме `rules`, а извлечение PDF
подменяется — тест не должен зависеть ни от ключа Anthropic, ни от хранилища.

    PYTHONPATH=. .venv/bin/python tests/verify_issue_review.py
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete, select

from src.core.config import settings
from src.domain import ai_review, issue_checks
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import Article, Issue, Journal

GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"
results: list[bool] = []

TAG = "AIREVIEW"


def check(name: str, got, want):
    ok = got == want
    results.append(ok)
    tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{tag}] {name}: got={got!r} want={want!r}")


async def _sweep(db):
    ids = (
        await db.execute(select(Article.id).where(Article.title.like(f"{TAG} %")))
    ).scalars().all()
    for stmt in (
        delete(Article).where(Article.id.in_(ids)),
        delete(Issue).where(Issue.title.like(f"{TAG} %")),
        delete(Journal).where(Journal.name.like(f"{TAG} %")),
    ):
        await db.execute(stmt)
    await db.commit()


def _pure_checks() -> None:
    print("\n--- разбор полей ---")
    check("диапазон «12-20»", issue_checks.page_range("12-20"), (12, 20))
    check("en dash «12–20»", issue_checks.page_range("12–20"), (12, 20))
    check("одна страница", issue_checks.page_range("7"), (7, 7))
    check("разрыв «1-5, 9-12»", issue_checks.page_range("1-5, 9-12"), (1, 12))
    check("мусор не разбирается", issue_checks.page_range("стр. первая"), None)
    check(
        "фамилии без инициалов",
        issue_checks.author_surnames("Иванов И.И., Петров П.П."),
        ["иванов", "петров"],
    )
    check(
        "DOI из текста с точкой на конце",
        issue_checks.find_dois("см. https://doi.org/10.1234/abc.def."),
        {"10.1234/abc.def"},
    )
    check("ISSN из колонтитула", issue_checks.find_issns("ISSN 2181-1024"), {"2181-1024"})

    print("\n--- сверка с PDF без модели ---")
    journal = Journal(name="Вестник", issn="2181-1024", printed_issn=None)
    article = Article(
        id=1,
        title="Влияние орошения на урожайность хлопчатника",
        authors="Иванов И.И.",
        pages="1-12",
        doi="10.1234/ruz.1",
    )
    problems = issue_checks.check_against_pdf(
        article,
        pdf_text=(
            "Вестник науки ISSN 2181-1024\n"
            "Влияние орошения на урожайность хлопчатника\n"
            "Иванов И.И.\nстр. 45"
        ),
        pdf_pages=12,
        journal=journal,
    )
    kinds = {(p.field, p.severity) for p in problems}
    check("«1-N» = объём файла — это major", ("pages", "major") in kinds, True)

    foreign = issue_checks.check_against_pdf(
        article,
        pdf_text="Другой журнал ISSN 1999-0001\n10.9999/other.5\nСовсем другая статья",
        pdf_pages=8,
        journal=journal,
    )
    kinds = {(p.field, p.severity) for p in foreign}
    check("чужой ISSN в PDF — major", ("journal", "major") in kinds, True)
    check("чужой DOI в PDF — major", ("doi", "major") in kinds, True)
    check("заголовка нет в PDF — major", ("title", "major") in kinds, True)

    clean = issue_checks.check_against_pdf(
        Article(
            id=2,
            title="Влияние орошения на урожайность хлопчатника",
            authors="Иванов И.И.",
            pages="45-52",
            doi="10.1234/ruz.2",
        ),
        pdf_text=(
            "Вестник науки ISSN 2181-1024\n"
            "Влияние орошения на урожайность хлопчатника\n"
            "Иванов И.И.\n10.1234/ruz.2\nстр. 45-52"
        ),
        pdf_pages=8,
        journal=journal,
    )
    check("совпадающие метаданные не дают major", [p for p in clean if p.severity == "major"], [])


def _fake_pdf(texts: dict[int, tuple[str, int]]):
    """Подмена извлечения PDF: тест не ходит ни в R2, ни в MinIO."""

    async def _stub(article: Article):
        text, pages = texts.get(article.id, ("", 0))
        if not text:
            return "", 0, "PDF без текстового слоя (скан)"
        return text, pages, None

    return _stub


async def main() -> int:
    _pure_checks()

    # Прогон только на правилах: модель в тесте не дёргаем.
    original_mode, original_pdf = settings.ISSUE_REVIEW_MODE, ai_review._pdf_text
    settings.ISSUE_REVIEW_MODE = "rules"

    suffix = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as db:
        await _sweep(db)

        journal = Journal(
            name=f"{TAG} journal {suffix}",
            slug=f"aireview-{suffix}",
            issn="2181-1024",
        )
        db.add(journal)
        await db.flush()
        issue = Issue(journal_id=journal.id, title=f"{TAG} issue", year=2026, issue="1")
        db.add(issue)
        await db.flush()

        def make(title: str, pages: str, doi: str | None = None) -> Article:
            article = Article(
                issue_id=issue.id,
                title=f"{TAG} {title}",
                authors="Иванов И.И.",
                pages=pages,
                doi=doi,
                pdf=f"https://files.example/{uuid.uuid4().hex}.pdf",
                slug=f"aireview-{uuid.uuid4().hex[:10]}",
                published=False,
                meta={"ai_review": {"status": ai_review.QUEUED, "attempts": 0}},
            )
            db.add(article)
            return article

        good_one = make("хлопчатник и орошение", "45-52")
        good_two = make("почвы Ферганской долины", "53-60")
        await db.commit()

        body = (
            "Вестник ISSN 2181-1024\n{title}\nИванов И.И.\nстраницы {pages}"
        )
        ai_review._pdf_text = _fake_pdf(
            {
                good_one.id: (body.format(title=good_one.title, pages="45-52"), 8),
                good_two.id: (body.format(title=good_two.title, pages="53-60"), 8),
            }
        )

        print("\n--- чистая очередь ---")
        result = await ai_review.review_issue(db, issue.id)
        await db.refresh(good_one)
        await db.refresh(good_two)
        await db.refresh(issue)
        check("вердикт по выпуску", result["status"], ai_review.PASSED)
        check("первая статья опубликована", good_one.published, True)
        check("вторая статья опубликована", good_two.published, True)
        check("выпуск не блокировался", (issue.meta or {}).get("blocked"), None)

        print("\n--- статья с пересечением страниц ---")
        bad = make("чужая статья", "45-52")
        await db.commit()
        ai_review._pdf_text = _fake_pdf(
            {bad.id: (body.format(title=bad.title, pages="45-52"), 8)}
        )
        result = await ai_review.review_issue(db, issue.id)
        await db.refresh(bad)
        await db.refresh(good_one)
        await db.refresh(issue)
        check("вердикт по выпуску", result["status"], ai_review.FLAGGED)
        check("новая статья помечена", (bad.meta or {})["ai_review"]["status"], "flagged")
        check("выпуск погашен", bool((issue.meta or {}).get("blocked")), True)
        check("ранее опубликованная снята", good_one.published, False)
        overlap = [
            p
            for p in (bad.meta or {})["ai_review"]["problems"]
            if p["field"] == "pages" and p["severity"] == "major"
        ]
        check("причина — пересечение страниц", bool(overlap), True)

        print("\n--- решение владельца ---")
        decision = await ai_review.apply_decision(
            db, issue.id, decision="publish", by="test"
        )
        await db.refresh(bad)
        await db.refresh(good_one)
        await db.refresh(issue)
        check("решение применилось", decision["status"], "ok")
        check("выпуск открыт", (issue.meta or {}).get("blocked"), None)
        check("снятая статья вернулась", good_one.published, True)
        check("спорная статья опубликована по решению", bad.published, True)

        repeat = await ai_review.apply_decision(
            db, issue.id, decision="keep_blocked", by="test"
        )
        check("повторное решение не проходит", repeat["status"], "already")

        print("\n--- уборка ---")
        await _sweep(db)
        left = (
            await db.execute(select(Article).where(Article.title.like(f"{TAG} %")))
        ).scalars().all()
        check("остатков фикстур не осталось", len(left), 0)

    settings.ISSUE_REVIEW_MODE = original_mode
    ai_review._pdf_text = original_pdf

    total_n, ok_n = len(results), sum(results)
    print(f"\n{'='*46}\nИтог: {ok_n}/{total_n} " + ("— всё зелёное" if ok_n == total_n else "— ЕСТЬ ПАДЕНИЯ"))
    return 0 if ok_n == total_n else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

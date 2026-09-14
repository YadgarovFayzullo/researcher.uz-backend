"""Разовый аудит уже опубликованных выпусков — отчёт, без санкций.

Автопроверка (`src/domain/ai_review.py`) работает с тем, что заливают СЕЙЧАС:
статья ложится черновиком, проверяется и либо публикуется, либо гасит выпуск.
К архиву тот же механизм применять нельзя — на живой базе правила находят
статьи с major в 29 выпусках из 100, и погасить их разом значило бы снести
с сайта половину каталога из-за ошибок, которым по несколько лет.

Поэтому здесь только чтение: пройти по выпускам, посчитать правила
(`src/domain/issue_checks.py`) и показать, где стоит разобраться руками.
Ничего не публикуется, не гасится и не пишется в базу.

PDF не скачивается: аудит идёт по метаданным и связям между статьями (дубли
заголовков и DOI, статьи с одинаковыми страницами) — именно это и находится
массово, а тянуть тысячи файлов из R2 ради разовой сводки незачем.

    PYTHONPATH=. .venv/bin/python scripts/audit_issues.py
    ... --journal-id 12      # только один журнал
    ... --limit 20           # сколько выпусков показать
    ... --send               # отправить сводку в Telegram владельцу
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict

from sqlalchemy import select

from src.core.config import settings
from src.domain import issue_checks
from src.infrastructure.external import telegram
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import Article, Issue, Journal

MAJOR = issue_checks.MAJOR


def _issue_label(issue: Issue) -> str:
    parts = [
        p
        for p in (
            str(issue.year) if issue.year else "",
            f"том {issue.volume}" if issue.volume else "",
            f"№{issue.issue}" if issue.issue else "",
        )
        if p
    ]
    return ", ".join(parts) or (issue.title or f"выпуск {issue.id}")


def _articles_url(issue: Issue) -> str:
    base = settings.ADMIN_BASE_URL.rstrip("/")
    return f"{base}/ru/admin/journals/publisher/{issue.journal_id}/articles/{issue.id}"


async def audit(journal_id: int | None) -> list[dict]:
    async with AsyncSessionLocal() as db:
        issues_q = select(Issue)
        if journal_id:
            issues_q = issues_q.where(Issue.journal_id == journal_id)
        issues = (await db.execute(issues_q)).scalars().all()
        journals = {
            j.id: j for j in (await db.execute(select(Journal))).scalars().all()
        }
        articles = (
            await db.execute(select(Article).where(Article.issue_id.isnot(None)))
        ).scalars().all()

    by_issue: dict[int, list[Article]] = defaultdict(list)
    for article in articles:
        by_issue[article.issue_id].append(article)

    report: list[dict] = []
    for issue in issues:
        rows = by_issue.get(issue.id, [])
        if not rows:
            continue
        found = issue_checks.check_issue(rows)
        major = {
            article_id: [p for p in problems if p.severity == MAJOR]
            for article_id, problems in found.items()
        }
        major = {k: v for k, v in major.items() if v}
        if not major:
            continue
        titles = {a.id: a.title for a in rows}
        report.append(
            {
                "issue": issue,
                "journal": journals.get(issue.journal_id),
                "total": len(rows),
                "flagged": major,
                "titles": titles,
            }
        )
    report.sort(key=lambda r: -len(r["flagged"]))
    return report


def render_console(report: list[dict], limit: int) -> None:
    total_articles = sum(len(r["flagged"]) for r in report)
    print(f"Выпусков с расхождениями: {len(report)}; статей: {total_articles}\n")
    for row in report[:limit]:
        issue, journal = row["issue"], row["journal"]
        print(
            f"— {(journal.name if journal else 'журнал не указан')[:50]} | "
            f"{_issue_label(issue)} (id {issue.id}): "
            f"{len(row['flagged'])} из {row['total']}"
        )
        for article_id, problems in list(row["flagged"].items())[:3]:
            title = (row["titles"].get(article_id) or "без названия")[:60]
            print(f"    · {title} (id {article_id})")
            print(f"      {problems[0].detail[:150]}")
        print()


def render_telegram(report: list[dict], limit: int) -> str:
    e = telegram.escape
    total_articles = sum(len(r["flagged"]) for r in report)
    lines = [
        "🔎 <b>Аудит архива: найдены расхождения</b>",
        "",
        f"Выпусков с проблемами: <b>{len(report)}</b> · "
        f"статей: <b>{total_articles}</b>",
        "",
        "Это разбор уже опубликованного. Ничего не снято и не заблокировано — "
        "решайте по каждому выпуску сами.",
        "",
    ]
    for row in report[:limit]:
        issue, journal = row["issue"], row["journal"]
        name = (journal.name if journal else "журнал не указан")[:45]
        lines.append(
            f"📗 <a href=\"{_articles_url(issue)}\">{e(name)} — "
            f"{e(_issue_label(issue))}</a>"
        )
        lines.append(
            f"     <i>{len(row['flagged'])} из {row['total']} статей</i>"
        )
        article_id, problems = next(iter(row["flagged"].items()))
        title = (row["titles"].get(article_id) or "без названия")[:70]
        lines.append(f"     ⚠️ {e(title)}: {e(problems[0].detail[:150])}")
        lines.append("")
    if len(report) > limit:
        lines.append(f"…и ещё {len(report) - limit} выпусков.")
    return "\n".join(lines)


async def deep_audit(issue_id: int, max_articles: int) -> dict:
    """Полная проверка одного выпуска: с загрузкой PDF и сверкой с текстом.

    Отличие от обычного аудита — файлы действительно скачиваются из хранилища,
    поэтому видно то, что по одной базе не увидеть: чужой журнал в колонтитуле,
    другой DOI внутри файла, заголовок, которого в PDF нет. Дорого по времени,
    поэтому только по одному выпуску и с потолком на число статей.
    """
    from src.domain import ai_review

    async with AsyncSessionLocal() as db:
        issue = (
            await db.execute(select(Issue).where(Issue.id == issue_id))
        ).scalars().first()
        if issue is None:
            return {"error": f"выпуск {issue_id} не найден"}
        journal = (
            await db.execute(select(Journal).where(Journal.id == issue.journal_id))
        ).scalars().first()
        rows = (
            await db.execute(select(Article).where(Article.issue_id == issue_id))
        ).scalars().all()
        queue = list(rows)[:max_articles]

        semaphore = asyncio.Semaphore(8)

        async def fetch(article: Article):
            async with semaphore:
                return article.id, await ai_review._pdf_text(article)

        print(f"скачиваем {len(queue)} PDF...")
        raw = dict(await asyncio.gather(*(fetch(a) for a in queue)))
        pdf_info = {aid: (t, p) for aid, (t, p, _) in raw.items()}
        skipped = {aid: r for aid, (_, _, r) in raw.items() if r}

        found = await issue_checks.collect(
            db, issue=issue, journal=journal, queue=queue, pdf_info=pdf_info
        )

    titles = {a.id: a.title for a in rows}
    major = {
        aid: [p for p in ps if p.severity == MAJOR] for aid, ps in found.items()
    }
    major = {k: v for k, v in major.items() if v}
    minor = {
        aid: [p for p in ps if p.severity != MAJOR] for aid, ps in found.items()
    }
    minor = {k: v for k, v in minor.items() if v}
    return {
        "issue": issue,
        "journal": journal,
        "total": len(rows),
        "checked": len(queue),
        "flagged": major,
        "minor": minor,
        "skipped": skipped,
        "titles": titles,
    }


def render_deep_telegram(result: dict) -> str:
    e = telegram.escape
    issue, journal = result["issue"], result["journal"]
    name = (journal.name if journal else "журнал не указан")[:60]
    lines = [
        "🔎 <b>Проверка выпуска по файлам</b>",
        "",
        f"📚 <b>Журнал:</b> {e(name)}",
        f"📗 <b>Выпуск:</b> <a href=\"{_articles_url(issue)}\">"
        f"{e(_issue_label(issue))}</a>",
        f"🔍 <b>Проверено:</b> {result['checked']} из {result['total']} статей "
        f"— с расхождениями {len(result['flagged'])}, "
        f"замечаний помельче у {len(result['minor'])}, "
        f"без текста в файле {len(result['skipped'])}",
        "",
        "Это разбор уже опубликованного: ничего не снято и не заблокировано.",
    ]
    if result["flagged"]:
        lines.append("")
        lines.append("<b>Серьёзные расхождения</b>")
        for index, (article_id, problems) in enumerate(
            result["flagged"].items(), start=1
        ):
            title = (result["titles"].get(article_id) or "без названия")[:90]
            base = settings.ADMIN_BASE_URL.rstrip("/")
            url = f"{base}/ru/admin/articles/edit/{article_id}?issueId={issue.id}"
            lines.append("")
            lines.append(f"{index}. <a href=\"{url}\">{e(title)}</a>")
            for problem in problems:
                lines.append(f"     ⚠️ {e(problem.detail[:200])}")
    else:
        lines.append("")
        lines.append("Серьёзных расхождений не найдено.")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--send", action="store_true", help="отправить в Telegram")
    parser.add_argument(
        "--deep",
        type=int,
        default=None,
        metavar="ISSUE_ID",
        help="полная проверка одного выпуска со скачиванием PDF",
    )
    parser.add_argument("--max-articles", type=int, default=40)
    args = parser.parse_args()

    if args.deep:
        result = await deep_audit(args.deep, args.max_articles)
        if result.get("error"):
            print(result["error"])
            return 1
        print(
            f"Проверено {result['checked']} из {result['total']}; "
            f"major у {len(result['flagged'])}, minor у {len(result['minor'])}, "
            f"без текста {len(result['skipped'])}"
        )
        for aid, problems in list(result["flagged"].items())[:10]:
            print(f"  · {(result['titles'].get(aid) or '')[:70]}")
            for p in problems[:3]:
                print(f"      {p.detail[:160]}")
        if args.send:
            if not settings.TELEGRAM_ENABLED:
                print("Telegram не настроен")
                return 1
            sent = await telegram.send_long(render_deep_telegram(result))
            print("Отправлено в Telegram" if sent else "Отправить не удалось")
        return 0

    report = await audit(args.journal_id)
    render_console(report, args.limit)

    if args.send:
        if not settings.TELEGRAM_ENABLED:
            print("Telegram не настроен — отчёт не отправлен")
            return 1
        message_id = await telegram.send_long(render_telegram(report, args.limit))
        print("Отправлено в Telegram" if message_id else "Отправить не удалось")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

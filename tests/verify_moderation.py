"""Снятая за нарушение статья гасит весь свой выпуск.

Редактор журнала завёл в номер чужие статьи (см. src/domain/moderation.py), и
реакция на это коллективная: одной статьи достаточно, чтобы выпуск целиком
ушёл с сайта до ручной проверки. Здесь стережём именно те свойства, которые
легко сломать правкой:

  * takedown снимает с публикации ВСЕ статьи выпуска, не только нарушителя;
  * отметки лежат в metadata (issues.blocked / articles.takedown);
  * заблокированный выпуск не виден публично (include_blocked=False) и виден
    в админке (умолчание True) — иначе его негде разблокировать;
  * unblock возвращает ровно то, что погасила блокировка: черновики, лежавшие
    снятыми до неё, и сама статья-нарушитель остаются снятыми;
  * повторный takedown в том же выпуске не затирает список отката.

Запуск: PYTHONPATH=. .venv/bin/python tests/verify_moderation.py
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete, select

from src.domain import moderation
from src.domain.issue import IssueDomain
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import Article, Issue, Journal

GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"
results: list[bool] = []

TAG = "MODER"


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


async def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as db:
        await _sweep(db)

        # ------------------------------------------------------- фикстуры
        journal = Journal(name=f"{TAG} journal {suffix}", slug=f"moder-{suffix}")
        db.add(journal)
        await db.flush()

        issue = Issue(journal_id=journal.id, title=f"{TAG} issue", year=2026)
        other = Issue(journal_id=journal.id, title=f"{TAG} issue clean", year=2026)
        db.add_all([issue, other])
        await db.flush()

        # Три опубликованные статьи + один черновик, снятый ЗАРАНЕЕ: он не
        # должен ни попасть в откат, ни всплыть при разблокировке.
        bad = Article(title=f"{TAG} нарушитель", issue_id=issue.id, published=True)
        good1 = Article(title=f"{TAG} нормальная 1", issue_id=issue.id, published=True)
        good2 = Article(title=f"{TAG} нормальная 2", issue_id=issue.id, published=True)
        draft = Article(title=f"{TAG} черновик", issue_id=issue.id, published=False)
        clean = Article(title=f"{TAG} чужой выпуск", issue_id=other.id, published=True)
        db.add_all([bad, good1, good2, draft, clean])
        await db.commit()

        # -------------------------------------------------------- takedown
        print("\n--- снятие статьи за нарушение ---")
        res = await moderation.takedown_article(
            db, bad, reason="чужая статья, страницы 1-N", by=None
        )
        for a in (bad, good1, good2, draft, clean):
            await db.refresh(a)
        await db.refresh(issue)

        check("нарушитель снят", bad.published, False)
        check("нормальные статьи выпуска сняты", [good1.published, good2.published], [False, False])
        check("статья соседнего выпуска не тронута", clean.published, True)
        check("у нарушителя отметка takedown", bool((bad.meta or {}).get("takedown")), True)
        check("выпуск помечен заблокированным", moderation.issue_is_blocked(issue), True)
        blocked = (issue.meta or {})["blocked"]
        check("в откате только погашенные блокировкой", sorted(blocked["article_ids"]), sorted([good1.id, good2.id]))
        check("черновик в откат не попал", draft.id in blocked["article_ids"], False)
        check("записан виновник", blocked["trigger_article_id"], bad.id)
        check("ответ ручки говорит о блокировке", bool(res["issue_blocked"]), True)

        # ----------------------------------------- видимость списка выпусков
        print("\n--- списки выпусков ---")
        public = await IssueDomain().list_issues(db, journal_id=journal.id, include_blocked=False)
        admin = await IssueDomain().list_issues(db, journal_id=journal.id)
        check("публично заблокированного выпуска нет", issue.id in [i["id"] for i in public], False)
        check("чистый выпуск публично остался", other.id in [i["id"] for i in public], True)
        check("в админке заблокированный виден", issue.id in [i["id"] for i in admin], True)

        # -------------------------- повторный takedown не затирает список отката
        print("\n--- повторное снятие в том же выпуске ---")
        await moderation.takedown_article(db, good1, reason="дубль DOI", by=None)
        await db.refresh(issue)
        check(
            "список отката прежний",
            sorted((issue.meta or {})["blocked"]["article_ids"]),
            sorted([good1.id, good2.id]),
        )

        # --------------------------------------------------------- unblock
        print("\n--- разблокировка ---")
        restored = await moderation.unblock_issue(db, issue)
        for a in (bad, good1, good2, draft):
            await db.refresh(a)
        await db.refresh(issue)

        check("вернули одну статью", restored, 1)
        check("нормальная статья опубликована", good2.published, True)
        check("нарушитель остался снятым", bad.published, False)
        check("второй нарушитель остался снятым", good1.published, False)
        check("черновик остался снятым", draft.published, False)
        check("метка блокировки снята", moderation.issue_is_blocked(issue), False)
        public = await IssueDomain().list_issues(db, journal_id=journal.id, include_blocked=False)
        check("выпуск снова публичен", issue.id in [i["id"] for i in public], True)

        # ------------------------------------------------------- уборка
        print("\n--- уборка ---")
        await _sweep(db)
        left = (
            await db.execute(select(Article).where(Article.title.like(f"{TAG} %")))
        ).scalars().all()
        check("остатков фикстур не осталось", len(left), 0)

    total_n, ok_n = len(results), sum(results)
    print(f"\n{'='*46}\nИтог: {ok_n}/{total_n} " + ("— всё зелёное" if ok_n == total_n else "— ЕСТЬ ПАДЕНИЯ"))
    return 0 if ok_n == total_n else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

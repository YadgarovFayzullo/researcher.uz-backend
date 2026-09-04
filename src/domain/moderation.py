"""Снятие материала с публикации и блокировка выпуска.

Зачем это написано. Редактор журнала завёл в свежий выпуск статьи, уже
опубликованные в других изданиях: PDF со чужими обложками, страницы «1-N»
(число страниц файла вместо пагинации выпуска), новые DOI на чужой текст, две
статьи — прямые дубли того, что уже лежало в этом же журнале за 2023 год. Для
Google Scholar это дубликаты с расходящимися метаданными, и санкция там
коллективная: снимают с индексации площадку, а не отдельную запись.

Поэтому и реакция коллективная. Одна статья, снятая за нарушение, гасит весь
свой выпуск: если редактор так завёл одну, доверия нет ко всему номеру, пока
владелец не просмотрит его руками. Гашение выпуска — это снятие с публикации
всех его статей, а не отдельный флаг видимости: тогда работают уже
существующие фильтры `published` в поиске, sitemap, OAI и лентах, и не нужно
добавлять проверку блокировки в десяток мест.

Состояние живёт в JSONB `issues.metadata.blocked` / `articles.metadata.takedown`
— как флаг демо-журнала в `journals.metadata.demo`; отдельная колонка ради
двух полей не нужна. В блокировке сохраняются id статей, которые погасила
именно она, — разблокировка возвращает ровно их и не публикует черновики,
которые лежали снятыми ещё до неё.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from src.infrastructure.persistence.models import Article, Issue


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_meta(obj: Any, key: str, value: Any) -> None:
    """Записать ключ в JSONB-колонку `metadata`.

    JSONB приезжает обычным dict, и правка вложенного значения не помечает
    атрибут грязным — SQLAlchemy такой UPDATE молча не отправит. Отсюда
    пересборка словаря и flag_modified.
    """
    meta = dict(obj.meta or {})
    if value is None:
        meta.pop(key, None)
    else:
        meta[key] = value
    obj.meta = meta
    flag_modified(obj, "meta")


def issue_is_blocked(issue: Issue) -> bool:
    return bool((issue.meta or {}).get("blocked"))


async def block_issue(
    db: AsyncSession,
    issue: Issue,
    *,
    reason: str,
    by: UUID | None,
    trigger_article_id: int | None = None,
) -> dict[str, Any]:
    """Погасить выпуск: снять с публикации все его статьи и пометить его.

    Повторный вызов на уже заблокированном выпуске оставляет исходную запись —
    иначе список `article_ids` затёрся бы пустым (статьи уже сняты) и
    разблокировка ничего бы не вернула.
    """
    if issue_is_blocked(issue):
        return dict((issue.meta or {})["blocked"])

    rows = (
        await db.execute(
            select(Article).where(
                Article.issue_id == issue.id, Article.published.is_(True)
            )
        )
    ).scalars().all()
    for article in rows:
        article.published = False

    blocked = {
        "at": _now(),
        "by": str(by) if by else None,
        "reason": reason,
        "trigger_article_id": trigger_article_id,
        # Статьи, снятые именно этой блокировкой, — для точного отката.
        "article_ids": [a.id for a in rows],
    }
    _set_meta(issue, "blocked", blocked)
    await db.commit()
    return blocked


async def unblock_issue(db: AsyncSession, issue: Issue) -> int:
    """Снять блокировку и вернуть в публикацию то, что она погасила.

    Статья-нарушитель в `article_ids` не попадает: её снимает takedown до
    блокировки, поэтому в выборку «сейчас опубликованных» она не входит и
    остаётся снятой. Её возвращают руками, разобравшись с метаданными.
    """
    blocked = (issue.meta or {}).get("blocked") or {}
    ids = [int(i) for i in blocked.get("article_ids") or []]
    restored = 0
    if ids:
        rows = (
            await db.execute(select(Article).where(Article.id.in_(ids)))
        ).scalars().all()
        for article in rows:
            # Снятое за нарушение обратно не поднимаем — только то, что
            # погасила сама блокировка.
            if (article.meta or {}).get("takedown"):
                continue
            article.published = True
            restored += 1
    _set_meta(issue, "blocked", None)
    await db.commit()
    return restored


async def takedown_article(
    db: AsyncSession,
    article: Article,
    *,
    reason: str,
    by: UUID | None,
) -> dict[str, Any]:
    """Снять статью с публикации как нарушение и погасить её выпуск."""
    article.published = False
    _set_meta(
        article,
        "takedown",
        {"at": _now(), "by": str(by) if by else None, "reason": reason},
    )
    await db.commit()

    blocked = None
    if article.issue_id:
        issue = (
            await db.execute(select(Issue).where(Issue.id == article.issue_id))
        ).scalars().first()
        if issue:
            blocked = await block_issue(
                db,
                issue,
                reason=reason,
                by=by,
                trigger_article_id=article.id,
            )
    return {"article_id": article.id, "issue_blocked": blocked}


async def restore_article(db: AsyncSession, article: Article) -> None:
    """Убрать отметку нарушения (выпуск при этом не разблокируется)."""
    _set_meta(article, "takedown", None)
    await db.commit()

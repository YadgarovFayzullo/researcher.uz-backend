"""Проверка документа на заимствования по базе платформы.

Как считается процент: документ раскладывается на отпечатки
(`src/domain/similarity.py`), они ищутся среди отпечатков статей платформы, и
доля найденных — это и есть «процент заимствований». Отпечатки прорежены
winnowing-ом, поэтому доля считается от того же прореженного множества, а не
от всех шинглов подряд — иначе проценты были бы несопоставимы.

Что этот движок ловит: повторные публикации одной статьи в разных журналах,
самоплагиат автора, перевод статьи в другую графику (узбекская латиница ↔
кириллица сводятся при нормализации). Чего он не ловит: интернет и чужие базы —
для этого нужны внешние источники (см. import-integration.md, слои 2 и 3).

Оговорка, важная для интерфейса: высокий процент сам по себе не приговор.
Обзорные статьи и методические разделы честно повторяют формулировки, поэтому
отчёт показывает не только цифру, но и совпавшие фрагменты — решение принимает
редактор.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.similarity import (
    MIN_WORDS,
    extract_fragment,
    fingerprints,
    normalize_words,
    total_shingles,
)
from src.infrastructure.persistence.models import (
    Article,
    ArticleFingerprint,
    ArticleText,
    Issue,
    Journal,
    PlagiarismCheck,
    PlagiarismMatch,
)

# Источники ниже этого процента в отчёт не попадают: пара случайно совпавших
# оборотов есть в любых двух статьях по одной теме.
MIN_SOURCE_SCORE = 1.0
# Сколько источников показываем. Больше десятка редактор всё равно не смотрит.
MAX_SOURCES = 10
# Сколько фрагментов приводим по каждому источнику.
MAX_FRAGMENTS = 5


class PlagiarismError(Exception):
    """Ошибка проверки, которую показываем пользователю."""


class NoTextLayer(PlagiarismError):
    """PDF без текстового слоя — скан. Проверять нечего."""


class TooShort(PlagiarismError):
    """Текста слишком мало, чтобы процент что-то значил."""


class PlagiarismDomain:
    # ------------------------------------------------------- индексация базы
    async def index_article(
        self, db: AsyncSession, article_id: int, text: str, *, commit: bool = True
    ) -> dict[str, Any]:
        """Сохранить текст статьи и её отпечатки (идемпотентно)."""
        words = normalize_words(text)
        prints = fingerprints(text)
        status = "ok" if words else "no_text_layer"

        await db.execute(
            pg_insert(ArticleText)
            .values(
                article_id=article_id,
                content=text,
                words_count=len(words),
                shingles_total=total_shingles(text),
                status=status,
                updated_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_update(
                index_elements=[ArticleText.article_id],
                set_={
                    "content": text,
                    "words_count": len(words),
                    "shingles_total": total_shingles(text),
                    "status": status,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        )

        # Переиндексация: старые отпечатки убираем целиком, иначе останутся
        # хвосты от прежней версии файла.
        await db.execute(
            delete(ArticleFingerprint).where(ArticleFingerprint.article_id == article_id)
        )
        if prints:
            await db.execute(
                pg_insert(ArticleFingerprint)
                .values(
                    [
                        {"article_id": article_id, "hash": h, "position": pos}
                        for h, pos in prints
                    ]
                )
                .on_conflict_do_nothing()
            )
        if commit:
            await db.commit()
        return {"words": len(words), "fingerprints": len(prints), "status": status}

    # ------------------------------------------------------------- проверка
    async def create_check(
        self,
        db: AsyncSession,
        *,
        journal_id: int | None,
        created_by,
        title: str | None,
        article_id: int | None = None,
    ) -> PlagiarismCheck:
        check = PlagiarismCheck(
            article_id=article_id,
            journal_id=journal_id,
            created_by=created_by,
            title=title,
            status="pending",
        )
        db.add(check)
        await db.commit()
        await db.refresh(check)
        return check

    async def run(
        self, db: AsyncSession, check: PlagiarismCheck, text: str
    ) -> PlagiarismCheck:
        """Посчитать заимствования и сохранить отчёт."""
        check.status = "running"
        await db.commit()

        try:
            words = normalize_words(text)
            if not words:
                raise NoTextLayer(
                    "В файле нет текстового слоя — похоже, это скан. "
                    "Проверить можно только документ с текстом."
                )
            if len(words) < MIN_WORDS:
                raise TooShort(
                    f"Слишком короткий текст ({len(words)} слов): для осмысленной "
                    f"проверки нужно хотя бы {MIN_WORDS}."
                )

            prints = fingerprints(text)
            by_hash = {h: pos for h, pos in prints}
            matches = await self._find_matches(db, check, by_hash, text)

            matched_hashes: set[int] = set()
            for row in matches:
                matched_hashes.update(row["hashes"])
            score = round(len(matched_hashes) * 100.0 / max(1, len(by_hash)), 2)

            await db.execute(
                delete(PlagiarismMatch).where(PlagiarismMatch.check_id == check.id)
            )
            for row in matches[:MAX_SOURCES]:
                db.add(
                    PlagiarismMatch(
                        check_id=check.id,
                        source_article_id=row["article_id"],
                        source_title=row["title"],
                        source_url=row["url"],
                        matched_shingles=len(row["hashes"]),
                        score=row["score"],
                        fragments=row["fragments"],
                    )
                )

            check.score = score
            check.words_count = len(words)
            check.details = {
                "fingerprints": len(by_hash),
                "sources_found": len(matches),
                "engine": "platform-db",
            }
            check.status = "done"
            check.finished_at = datetime.now(timezone.utc)
            await db.commit()
        except PlagiarismError as e:
            check.status = "failed"
            check.error = str(e)
            check.finished_at = datetime.now(timezone.utc)
            await db.commit()
            raise
        except Exception as e:  # noqa: BLE001 — в отчёт должна попасть любая поломка
            check.status = "failed"
            check.error = str(e)[:500]
            check.finished_at = datetime.now(timezone.utc)
            await db.commit()
            raise

        await db.refresh(check)
        return check

    async def _find_matches(
        self,
        db: AsyncSession,
        check: PlagiarismCheck,
        by_hash: dict[int, int],
        text: str,
    ) -> list[dict[str, Any]]:
        """Источники, чьи отпечатки пересекаются с документом."""
        if not by_hash:
            return []

        hashes = list(by_hash.keys())
        stmt = (
            select(
                ArticleFingerprint.article_id,
                ArticleFingerprint.hash,
                ArticleFingerprint.position,
            )
            .where(ArticleFingerprint.hash.in_(hashes))
        )
        # Саму себя статья не «заимствует»: при перепроверке уже
        # проиндексированной статьи её собственные отпечатки надо исключить.
        if check.article_id:
            stmt = stmt.where(ArticleFingerprint.article_id != check.article_id)

        rows = (await db.execute(stmt)).all()
        if not rows:
            return []

        grouped: dict[int, dict[str, Any]] = {}
        for article_id, hash_value, position in rows:
            entry = grouped.setdefault(
                article_id, {"hashes": set(), "positions": []}
            )
            entry["hashes"].add(hash_value)
            entry["positions"].append((by_hash[hash_value], position))

        titles = await self._titles(db, list(grouped.keys()))
        result: list[dict[str, Any]] = []
        for article_id, entry in grouped.items():
            score = round(len(entry["hashes"]) * 100.0 / max(1, len(by_hash)), 2)
            if score < MIN_SOURCE_SCORE:
                continue
            meta = titles.get(article_id, {})
            result.append(
                {
                    "article_id": article_id,
                    "title": meta.get("title"),
                    "url": meta.get("url"),
                    "score": score,
                    "hashes": entry["hashes"],
                    "fragments": self._fragments(text, entry["positions"]),
                }
            )
        result.sort(key=lambda row: row["score"], reverse=True)
        return result

    def _fragments(
        self, text: str, positions: Sequence[tuple[int, int]]
    ) -> list[dict[str, Any]]:
        """Совпавшие куски документа — то, что редактор реально читает.

        Позиции идут вразнобой; соседние склеиваем, иначе один заимствованный
        абзац показался бы десятком почти одинаковых обрывков.
        """
        doc_positions = sorted({pos for pos, _ in positions})
        merged: list[int] = []
        for pos in doc_positions:
            if merged and pos - merged[-1] < 12:
                continue
            merged.append(pos)

        out: list[dict[str, Any]] = []
        for pos in merged[:MAX_FRAGMENTS]:
            fragment = extract_fragment(text, pos)
            if fragment:
                out.append({"position": pos, "text": fragment})
        return out

    async def _titles(
        self, db: AsyncSession, article_ids: list[int]
    ) -> dict[int, dict[str, Any]]:
        """Название и ссылка на статью-источник для отчёта."""
        if not article_ids:
            return {}
        rows = (
            await db.execute(
                select(
                    Article.id,
                    Article.title,
                    Article.slug,
                    Journal.name.label("journal_name"),
                )
                .outerjoin(Issue, Issue.id == Article.issue_id)
                .outerjoin(Journal, Journal.id == Issue.journal_id)
                .where(Article.id.in_(article_ids))
            )
        ).all()
        return {
            row.id: {
                "title": " — ".join(filter(None, [row.title, row.journal_name])),
                "url": f"https://researcher.uz/uz/article/{row.slug}" if row.slug else None,
            }
            for row in rows
        }

    # --------------------------------------------------------------- чтение
    async def get_check(self, db: AsyncSession, check_id: int) -> PlagiarismCheck | None:
        return (
            await db.execute(select(PlagiarismCheck).where(PlagiarismCheck.id == check_id))
        ).scalars().first()

    async def list_checks(
        self, db: AsyncSession, *, journal_id: int | None = None, limit: int = 50
    ) -> list[PlagiarismCheck]:
        stmt = select(PlagiarismCheck).order_by(PlagiarismCheck.created_at.desc()).limit(limit)
        if journal_id is not None:
            stmt = stmt.where(PlagiarismCheck.journal_id == journal_id)
        return list((await db.execute(stmt)).scalars().all())

    async def matches(self, db: AsyncSession, check_id: int) -> list[PlagiarismMatch]:
        return list(
            (
                await db.execute(
                    select(PlagiarismMatch)
                    .where(PlagiarismMatch.check_id == check_id)
                    .order_by(PlagiarismMatch.score.desc())
                )
            )
            .scalars()
            .all()
        )

    async def coverage(self, db: AsyncSession) -> dict[str, int]:
        """Насколько база готова к проверкам — для отчёта владельцу."""
        total = int((await db.execute(select(func.count(Article.id)))).scalar_one())
        indexed = int(
            (
                await db.execute(
                    select(func.count(ArticleText.article_id)).where(
                        ArticleText.status == "ok"
                    )
                )
            ).scalar_one()
        )
        scans = int(
            (
                await db.execute(
                    select(func.count(ArticleText.article_id)).where(
                        ArticleText.status == "no_text_layer"
                    )
                )
            ).scalar_one()
        )
        return {"articles": total, "indexed": indexed, "scans": scans}

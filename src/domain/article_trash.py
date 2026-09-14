"""Корзина статей: удалённая статья 30 дней лежит снимком и восстанавливается.

Зачем: редактор Inter Education удалил выпуск, завёл его заново и перезалил
те же 67 статей. Старые копии пришлось удалить, и вместе с ними ушли 1242
просмотра — вернуть их удалось только из ручного бэкапа. Удаление статьи
необратимо по своей природе (FK без каскада, связанные строки сносятся явно),
поэтому страховка — снимок в той же транзакции, что и удаление.

Почему снимок, а не флаг `deleted_at` на статье: флаг пришлось бы учитывать в
каждом чтении статей (поиск, sitemap, OAI, ленты, статистика, карточки
авторов), и одно забытое место вернуло бы удалённое на сайт. Снимок не трогает
ни одного чтения.

Устройство:
  * `trash_articles` — один INSERT…SELECT: строка статьи и зависимые строки
    через `to_jsonb`. Зовётся из `ArticleDomain.delete_articles` до удаления.
  * `restore` — вставка обратно через `jsonb_populate_recordset` только тех
    колонок, что есть в таблице СЕЙЧАС: снимок месячной давности переживает
    добавление колонок. id статьи сохраняется — ссылки на неё не ломаются.
  * `purge_expired` — чистка старше RETENTION_DAYS, фоном (trash_purge.py).
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

RETENTION_DAYS = 30

# tsvector'ы пересчитывает триггер articles_tsv_update на INSERT — хранить их
# в снимке незачем. Эмбеддинг храним: пересчитывать его дорого.
_TSVECTOR_KEYS = ["search_vector", "document_ru", "document_en", "document_uz"]

# (таблица, колонка со ссылкой на статью): строки, которые уходят вместе со
# статьёй — явно в delete_articles или каскадом FK. Отпечатков антиплагиата
# здесь нет: их тысячи на статью, и они однозначно пересчитываются из текста
# (article_texts), что restore и делает.
DEPENDENT_TABLES: tuple[tuple[str, str], ...] = (
    ("article_authors", "article_id"),
    ("article_references", "article_id"),
    ("article_interactions", "article_id"),
    ("external_citations", "article_id"),
    ("saved_articles", "article_id"),
    ("article_texts", "article_id"),
    ("crossref_deposits", "article_id"),
)

# Чужие строки, в которых удаление обнуляет ссылку на статью (ON DELETE SET
# NULL или явный UPDATE в delete_articles). Запоминаем их id и при
# восстановлении возвращаем ссылку, если её никто не занял.
RELINK_TABLES: tuple[tuple[str, str], ...] = (
    ("article_references", "cited_article_id"),
    ("import_items", "article_id"),
    ("plagiarism_checks", "article_id"),
)


class TrashNotFound(Exception):
    pass


class TrashConflict(Exception):
    """Восстановить нельзя; текст показывается владельцу как есть."""


def _obj(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def trash_articles(
    db: AsyncSession, ids: Sequence[int], *, deleted_by: uuid.UUID | str | None = None
) -> int:
    """Положить снимки статей в корзину. Без commit — это часть удаления."""
    ids = list(dict.fromkeys(int(i) for i in ids))
    if not ids:
        return 0
    dependents = ", ".join(
        f"'{table}', (SELECT coalesce(jsonb_agg(to_jsonb(d)), CAST('[]' AS jsonb))"
        f" FROM {table} d WHERE d.{col} = a.id)"
        for table, col in DEPENDENT_TABLES
    )
    relinks = ", ".join(
        f"'{table}.{col}', (SELECT coalesce(jsonb_agg(d.id), CAST('[]' AS jsonb))"
        f" FROM {table} d WHERE d.{col} = a.id)"
        for table, col in RELINK_TABLES
    )
    res = await db.execute(
        text(
            f"""
            INSERT INTO article_trash
                (article_id, title, slug, issue_id, journal_id, journal_name,
                 deleted_by, snapshot)
            SELECT a.id, a.title, a.slug, a.issue_id, i.journal_id, j.name,
                   CAST(:deleted_by AS uuid),
                   jsonb_build_object(
                       'version', 1,
                       'article', to_jsonb(a) - CAST(:drop AS text[]),
                       'dependents', jsonb_build_object({dependents}),
                       'relinks', jsonb_build_object({relinks})
                   )
            FROM articles a
            LEFT JOIN issues i ON i.id = a.issue_id
            LEFT JOIN journals j ON j.id = i.journal_id
            WHERE a.id = ANY(CAST(:ids AS bigint[]))
            """
        ),
        {
            "ids": ids,
            "drop": _TSVECTOR_KEYS,
            "deleted_by": str(deleted_by) if deleted_by else None,
        },
    )
    return res.rowcount or 0


async def list_trash(
    db: AsyncSession,
    *,
    q: str | None = None,
    journal_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    where = ["true"]
    params: dict[str, Any] = {"limit": limit, "offset": offset, "days": RETENTION_DAYS}
    if q:
        where.append("(t.title ILIKE :q OR t.snapshot->'article'->>'authors' ILIKE :q)")
        params["q"] = f"%{q}%"
    if journal_id:
        where.append("t.journal_id = :journal_id")
        params["journal_id"] = journal_id
    cond = " AND ".join(where)

    total = await db.scalar(text(f"SELECT count(*) FROM article_trash t WHERE {cond}"), params)
    rows = (
        await db.execute(
            text(
                f"""
                SELECT t.id, t.article_id, t.title, t.slug, t.issue_id,
                       t.journal_id, t.journal_name, t.deleted_at,
                       t.deleted_at + make_interval(days => :days) AS expires_at,
                       u.email AS deleted_by_email, p.full_name AS deleted_by_name,
                       t.snapshot->'article'->>'authors' AS authors,
                       t.snapshot->'article'->>'pages' AS pages,
                       CAST(t.snapshot->'article'->>'published' AS boolean) AS published,
                       CAST(t.snapshot->'article'->>'views_count' AS integer) AS views,
                       (i.id IS NOT NULL) AS issue_exists,
                       concat_ws(' ', i.year, 'т.' || i.volume, '№' || i.issue) AS issue_label
                FROM article_trash t
                LEFT JOIN issues i ON i.id = t.issue_id
                LEFT JOIN profiles p ON p.id = t.deleted_by
                LEFT JOIN users u ON u.id = t.deleted_by
                WHERE {cond}
                ORDER BY t.deleted_at DESC, t.id DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
    ).mappings().all()
    items = [dict(r) for r in rows]
    for item in items:
        # Выпуск без выпуска (standalone-публикация) восстановим всегда.
        if item["issue_id"] is None:
            item["issue_exists"] = True
            item["issue_label"] = None
    return {"items": items, "total": total or 0, "retention_days": RETENTION_DAYS}


async def _columns(db: AsyncSession, table: str) -> set[str]:
    rows = await db.execute(
        text(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = :t"
            " AND is_generated = 'NEVER'"
        ),
        {"t": table},
    )
    return set(rows.scalars().all())


async def _insert_rows(db: AsyncSession, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    present = {key for row in rows for key in row}
    cols = sorted(present & await _columns(db, table))
    collist = ", ".join(f'"{c}"' for c in cols)
    # OVERRIDING SYSTEM VALUE: id у articles/saved_articles/crossref_deposits —
    # GENERATED ALWAYS, а вернуть нужно прежний. На таблицах без identity
    # Postgres это предложение просто игнорирует.
    await db.execute(
        text(
            f"INSERT INTO {table} ({collist}) OVERRIDING SYSTEM VALUE"
            f" SELECT {collist} FROM jsonb_populate_recordset(CAST(NULL AS {table}),"
            f" CAST(:rows AS jsonb))"
        ),
        {"rows": json.dumps(rows, ensure_ascii=False)},
    )


async def _existing(db: AsyncSession, table: str, ids: set) -> set[str]:
    ids = {str(i) for i in ids if i is not None}
    if not ids:
        return set()
    rows = await db.execute(
        text(f"SELECT CAST(id AS text) FROM {table} WHERE CAST(id AS text) = ANY(:ids)"),
        {"ids": list(ids)},
    )
    return set(rows.scalars().all())


async def _null_missing(
    db: AsyncSession, rows: list[dict], key: str, table: str
) -> int:
    """Обнулить в строках ссылку `key`, если в `table` такой строки больше нет."""
    alive = await _existing(db, table, {r.get(key) for r in rows})
    dropped = 0
    for row in rows:
        if row.get(key) is not None and str(row[key]) not in alive:
            row[key] = None
            dropped += 1
    return dropped


async def restore(db: AsyncSession, trash_id: int) -> dict[str, Any]:
    """Вернуть статью из корзины. Всё одной транзакцией."""
    from src.domain.moderation import issue_is_blocked
    from src.domain.plagiarism import PlagiarismDomain
    from src.infrastructure.persistence.models import Issue

    row = (
        await db.execute(
            text("SELECT id, article_id, snapshot FROM article_trash WHERE id = :id FOR UPDATE"),
            {"id": trash_id},
        )
    ).mappings().first()
    if not row:
        raise TrashNotFound()

    snap = _obj(row["snapshot"])
    article: dict = snap["article"]
    article_id = int(row["article_id"])
    deps: dict[str, list[dict]] = snap.get("dependents") or {}
    warnings: list[str] = []

    if await db.scalar(text("SELECT 1 FROM articles WHERE id = :id"), {"id": article_id}):
        raise TrashConflict(f"Статья с id {article_id} уже есть в базе.")
    slug = article.get("slug")
    if slug and await db.scalar(text("SELECT 1 FROM articles WHERE slug = :s"), {"s": slug}):
        raise TrashConflict(
            "Адрес статьи уже занят другой статьёй — похоже, её загрузили заново."
        )

    issue_id = article.get("issue_id")
    if issue_id is not None:
        issue = await db.get(Issue, int(issue_id))
        if issue is None:
            # Без выпуска статья повисла бы опубликованной сиротой — ровно та
            # история, из-за которой корзина и появилась.
            raise TrashConflict(
                "Выпуск статьи удалён. Заведите выпуск заново и перенесите статью"
                " вручную или обратитесь к разработчику."
            )
        if issue_is_blocked(issue) and article.get("published"):
            article["published"] = False
            warnings.append("Выпуск заблокирован — статья восстановлена неопубликованной.")

    for key, table in (
        ("section_id", "conference_sections"),
        ("publisher_id", "publishers"),
        ("user_id", "profiles"),
        ("admin_id", "profiles"),
    ):
        if await _null_missing(db, [article], key, table):
            warnings.append(f"Связь {key} потеряна: запись удалена.")

    await _insert_rows(db, "articles", [article])

    # Ссылки, которые могли исчезнуть за месяц: карточку автора пересобрали,
    # процитированную статью удалили, пользователь удалил аккаунт.
    authors = deps.get("article_authors") or []
    await _null_missing(db, authors, "author_id", "authors")
    await _null_missing(db, authors, "profile_id", "profiles")
    refs = deps.get("article_references") or []
    await _null_missing(db, refs, "cited_article_id", "articles")
    saved = deps.get("saved_articles") or []
    alive_users = await _existing(db, "users", {r.get("user_id") for r in saved})
    deps["saved_articles"] = [r for r in saved if str(r.get("user_id")) in alive_users]

    for table, _col in DEPENDENT_TABLES:
        rows = deps.get(table) or []
        if not rows:
            continue
        try:
            async with db.begin_nested():
                await _insert_rows(db, table, rows)
        except Exception:
            # Статья важнее хвостов: не вернувшиеся строки называем владельцу,
            # а не отменяем всё восстановление.
            warnings.append(f"Не восстановлено: {table} ({len(rows)} строк).")

    texts = deps.get("article_texts") or []
    if texts and texts[0].get("status") == "ok" and texts[0].get("content"):
        await PlagiarismDomain().index_article(
            db, article_id, texts[0]["content"], commit=False
        )

    for key, ids in (snap.get("relinks") or {}).items():
        table, col = key.split(".", 1)
        if ids:
            await db.execute(
                text(
                    f"UPDATE {table} SET {col} = :aid"
                    f" WHERE CAST(id AS text) = ANY(:ids) AND {col} IS NULL"
                ),
                {"aid": article_id, "ids": [str(i) for i in ids]},
            )

    await db.execute(text("DELETE FROM article_trash WHERE id = :id"), {"id": trash_id})
    journal_slug = None
    if issue_id is not None:
        journal_slug = await db.scalar(
            text(
                "SELECT j.slug FROM issues i JOIN journals j ON j.id = i.journal_id"
                " WHERE i.id = :id"
            ),
            {"id": int(issue_id)},
        )
    await db.commit()
    return {
        "article_id": article_id,
        "slug": slug,
        "journal_slug": journal_slug,
        "published": bool(article.get("published")),
        "warnings": warnings,
    }


async def purge_expired(db: AsyncSession) -> int:
    res = await db.execute(
        text(
            "DELETE FROM article_trash"
            " WHERE deleted_at < now() - make_interval(days => :days)"
        ),
        {"days": RETENTION_DAYS},
    )
    await db.commit()
    return res.rowcount or 0

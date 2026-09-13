"""Собрать карточки авторов из подписей под статьями.

Делает три вещи за один проход:

1. Разбирает текстовое поле `articles.authors` у статей, где структурных строк
   `article_authors` нет. На проде таких 3831 из 6625 — без этого шага больше
   половины корпуса осталась бы вне карточек.
2. Группирует все подписи по ключу личности (`src/domain/author_names`) и
   заводит/обновляет строки `authors`.
3. Проставляет `article_authors.author_id` и денормализованный `works_count`.

Скрипт идемпотентен: повторный запуск не плодит дублей и не трогает уже
связанные строки. Запускать после каждого крупного импорта.

    python scripts/backfill_authors.py --dry-run    # только показать, что выйдет
    python scripts/backfill_authors.py              # записать
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select, update  # noqa: E402

from src.domain.author_names import (  # noqa: E402
    clean,
    display_name,
    identity_key,
    slug_for,
    split_authors,
)
from src.infrastructure.persistence.db import AsyncSessionLocal  # noqa: E402
from src.domain.demo import article_is_not_demo  # noqa: E402
from src.infrastructure.persistence.models import (  # noqa: E402
    Article,
    ArticleAuthor,
    Author,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill_authors")


async def parse_text_authors(db) -> int:
    """Шаг 1: `articles.authors` → строки `article_authors`."""
    have_rows = set(
        (await db.execute(select(ArticleAuthor.article_id).distinct())).scalars().all()
    )
    rows = (
        await db.execute(
            select(Article.id, Article.authors).where(
                Article.authors.isnot(None), Article.authors != ""
            )
        )
    ).all()

    created = 0
    for article_id, text in rows:
        if article_id in have_rows:
            continue
        names = split_authors(text)
        for order, name in enumerate(names, start=1):
            if not name:
                continue
            created += 1
            db.add(
                ArticleAuthor(
                    article_id=article_id, author_order=order, author_name=name
                )
            )
    # Пишем даже в dry-run: иначе шаг 2 не увидит этих подписей и покажет цифры
    # вдвое меньше настоящих. Откат — один на весь прогон, в main().
    await db.flush()
    logger.info("шаг 1: строк article_authors из текстового поля — %d", created)
    return created


async def build_cards(db) -> tuple[int, int, int]:
    """Шаги 2-3: карточки авторов и связь со строками подписей."""
    rows = (
        await db.execute(
            select(
                ArticleAuthor.id,
                ArticleAuthor.article_id,
                ArticleAuthor.author_name,
                ArticleAuthor.orcid,
                ArticleAuthor.profile_id,
            )
        )
    ).all()

    groups: dict[str, dict] = defaultdict(
        lambda: {"names": [], "rows": [], "articles": set(), "orcid": None, "profile": None}
    )
    skipped = 0
    for row_id, article_id, name, orcid, profile_id in rows:
        key = identity_key(name or "")
        if not key:
            # Имя из одного слова («Sevara»): за ним стоят разные люди, общая
            # карточка была бы ложным слиянием. Такая подпись остаётся текстом.
            skipped += 1
            continue
        g = groups[key]
        g["names"].append(clean(name or ""))
        g["rows"].append(row_id)
        g["articles"].add(article_id)
        g["orcid"] = g["orcid"] or orcid
        g["profile"] = g["profile"] or profile_id

    # works_count считаем по ВИДИМЫМ работам: карточка индексируется по порогу
    # в 2 работы, и черновики с демо-статьями не должны его перешагивать —
    # иначе в выдачу уйдёт страница, на которой показывать нечего.
    visible = set(
        (
            await db.execute(
                select(Article.id).where(
                    Article.published.is_(True), article_is_not_demo()
                )
            )
        )
        .scalars()
        .all()
    )

    existing = {
        a.name_key: a for a in (await db.execute(select(Author))).scalars().all()
    }
    taken_slugs = {a.slug for a in existing.values()}

    created = updated = 0
    for key, g in groups.items():
        title = display_name(g["names"])
        works = len(g["articles"] & visible)
        author = existing.get(key)
        if author is None:
            slug = unique_slug(slug_for(key, title), taken_slugs)
            taken_slugs.add(slug)
            created += 1
            author = Author(
                name_key=key,
                slug=slug,
                display_name=title,
                orcid=g["orcid"],
                profile_id=g["profile"],
                works_count=works,
            )
            db.add(author)
        else:
            # Slug не трогаем никогда: он уже в выдаче и во внешних ссылках.
            changed = author.display_name != title or author.works_count != works
            if changed:
                updated += 1
            author.display_name = title
            author.works_count = works
            author.orcid = author.orcid or g["orcid"]
            author.profile_id = author.profile_id or g["profile"]

    await db.flush()
    ids = {
        a.name_key: a.id for a in (await db.execute(select(Author))).scalars().all()
    }
    for key, g in groups.items():
        await db.execute(
            update(ArticleAuthor)
            .where(ArticleAuthor.id.in_(g["rows"]))
            .values(author_id=ids[key])
        )
    logger.info(
        "шаг 2: карточек новых — %d, обновлено — %d, подписей без карточки — %d",
        created,
        updated,
        skipped,
    )
    return created, updated, skipped


def unique_slug(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


async def main(dry_run: bool) -> int:
    async with AsyncSessionLocal() as db:
        await parse_text_authors(db)
        created, updated, skipped = await build_cards(db)
        total_now = (await db.execute(select(func.count(Author.id)))).scalar_one()
        indexable_now = (
            await db.execute(
                select(func.count(Author.id)).where(Author.works_count >= 2)
            )
        ).scalar_one()
        logger.info(
            "итог: карточек %d, из них с ≥2 работами %d", total_now, indexable_now
        )
        if dry_run:
            await db.rollback()
            logger.info("dry-run: транзакция откачена, в базе ничего не изменилось")
        else:
            await db.commit()
            total = (await db.execute(select(func.count(Author.id)))).scalar_one()
            indexable = (
                await db.execute(
                    select(func.count(Author.id)).where(Author.works_count >= 2)
                )
            ).scalar_one()
            logger.info("готово: карточек %d, из них с ≥2 работами %d", total, indexable)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="показать, ничего не писать")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.dry_run)))

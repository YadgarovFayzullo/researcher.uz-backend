"""Разовая чистка: у статьи `data` = день импорта вместо года выпуска.

Форма добавления статьи (`AddArticle.tsx`) не собирает поле «дата публикации»
— только выпуск (год/том/номер). `create_article` до правки в
`src/domain/article.py` не подставлял `data` сам, и колонка уезжала в
server_default `CURRENT_DATE`, то есть в день, когда редактор нажал
«Сохранить». На фронте `publicationDateForScholar` (src/lib/seo.ts) доверяет
`articles.data` как точной дате, если это не «1 января» — и статья из выпуска
2024 года показывала Google Scholar «опубликовано 2026/7/10» (см. скриншот
19.09: `citation_date` = день сегодняшнего клика редактора).

Правка в create_article останавливает НОВЫЕ порчи; этот скрипт чинит уже
накопленные — на проде 1129 строк, копится с 18.06.2026 по нарастающей.

Строка считается испорченной, когда год `articles.data` не совпадает с годом
её выпуска, а сама дата совпадает с днём создания статьи — то есть это
дефолт, а не то, что кто-то ввёл руками. Правим на 1 января года выпуска —
тот же плейсхолдер, что и у архивного импортёра (`importing.py`); фронт уже
умеет отличать «1 января» от точной даты и откатывается на один год.

По умолчанию ничего не пишет — только показывает план:

    PYTHONPATH=. .venv/bin/python scripts/fix_article_data_year.py
    PYTHONPATH=. .venv/bin/python scripts/fix_article_data_year.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from src.infrastructure.persistence.db import AsyncSessionLocal  # noqa: E402
from src.infrastructure.persistence.models import Article, Issue  # noqa: E402


async def main(apply: bool) -> None:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Article.id, Article.slug, Article.data, Issue.year)
                .join(Issue, Issue.id == Article.issue_id)
                .where(
                    Issue.year.isnot(None),
                    Article.data.isnot(None),
                )
            )
        ).all()

        this_year = datetime.now(timezone.utc).year
        fixes: list[tuple[int, str, date]] = []
        for article_id, slug, art_data, issue_year in rows:
            if art_data.year == issue_year:
                continue
            # Год выпуска ещё не наступил — не тот случай, пропускаем молча
            # (в проде таких не должно быть, но на всякий).
            if issue_year >= this_year:
                continue
            new_date = date(issue_year, 1, 1)
            fixes.append((article_id, slug, new_date))

        print(f"Статей с расхождением год(data) != год(выпуска): {len(fixes)}")
        for article_id, slug, new_date in fixes[:20]:
            print(f"  id={article_id:<6} {slug[:60]:<60} -> {new_date}")
        if len(fixes) > 20:
            print(f"  … и ещё {len(fixes) - 20}")

        if not apply:
            print("\nПлан. Для записи добавьте --apply.")
            return

        for article_id, _slug, new_date in fixes:
            article = await db.get(Article, article_id)
            article.data = new_date
        await db.commit()
        print(f"\nЗаписано: {len(fixes)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.apply))

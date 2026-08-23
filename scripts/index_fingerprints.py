"""Индексация статей для проверки на заимствования.

Скачивает PDF статьи, извлекает текст и считает отпечатки
(`src/domain/similarity.py`). Без этого проверка сравнивать не с чем.

Идемпотентно: по умолчанию берёт только статьи без отпечатков, поэтому запуск
можно повторять и догонять новые публикации. `--force` переиндексирует всё
заново (нужно, если поменялись параметры шинглов).

    PYTHONPATH=. .venv/bin/python scripts/index_fingerprints.py --limit 100
    ... --all                # пройти всю базу
    ... --article 12345      # одну статью
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

from sqlalchemy import select

from src.domain.plagiarism import PlagiarismDomain
from src.infrastructure.pdf_text import pdf_to_checkable_text
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import Article, ArticleText
from src.infrastructure.storage import StorageNotConfigured, key_from_url, storage


async def fetch_pdf(pdf_url: str) -> bytes | None:
    """PDF из нашего хранилища. Внешние ссылки не трогаем — это не наш файл."""
    key = key_from_url(pdf_url, default_prefix="pdfs")
    if not key:
        return None
    try:
        body, _content_type = await asyncio.to_thread(storage.get, key)
        return body
    except StorageNotConfigured:
        raise
    except Exception:
        return None


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--all", action="store_true", help="обойти всю базу")
    p.add_argument("--article", type=int, help="только одна статья по id")
    p.add_argument("--force", action="store_true", help="переиндексировать уже готовые")
    args = p.parse_args()

    domain = PlagiarismDomain()
    done = skipped = failed = scans = 0
    started = time.time()

    async with AsyncSessionLocal() as db:
        stmt = select(Article.id, Article.pdf, Article.title).where(Article.pdf.isnot(None))
        if args.article:
            stmt = stmt.where(Article.id == args.article)
        elif not args.force:
            # Уже проиндексированные пропускаем — скрипт догоняет только новое.
            indexed = select(ArticleText.article_id)
            stmt = stmt.where(Article.id.notin_(indexed))
        stmt = stmt.order_by(Article.id)
        if not args.all and not args.article:
            stmt = stmt.limit(args.limit)

        rows = (await db.execute(stmt)).all()
        print(f"к обработке: {len(rows)} статей")

        for index, (article_id, pdf_url, title) in enumerate(rows, start=1):
            try:
                data = await fetch_pdf(pdf_url)
                if not data:
                    skipped += 1
                    continue
                text = pdf_to_checkable_text(data)
                result = await domain.index_article(db, article_id, text)
                if result["status"] == "no_text_layer":
                    scans += 1
                else:
                    done += 1
            except StorageNotConfigured as e:
                print("Хранилище не настроено:", e)
                return 1
            except Exception as e:  # одна битая статья не должна ронять проход
                failed += 1
                print(f"  ! {article_id} {(title or '')[:40]}: {e}")

            if index % 25 == 0:
                print(
                    f"  {index}/{len(rows)} — готово {done}, сканов {scans}, "
                    f"пропущено {skipped}, ошибок {failed}, "
                    f"{time.time() - started:.0f}с"
                )

    print(
        f"\nИтог: проиндексировано {done}, сканов без текста {scans}, "
        f"пропущено {skipped}, ошибок {failed}, за {time.time() - started:.0f}с"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

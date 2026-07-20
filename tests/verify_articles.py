"""Проверка ресурса статей после расширения схем и домена (8b-подготовка).

Что стережём:
  * create/update/delete вообще работают (update_article/delete_article в домене
    не было, хотя роутер их вызывал — PATCH и DELETE падали с AttributeError);
  * PATCH с exclude_unset не затирает поля, которых не было в теле;
  * поля standalone-публикаций (publication_type, publisher_id, metadata, ...)
    доезжают до БД — раньше схема их молча отбрасывала;
  * фильтры и пагинация списка считают то же, что и count;
  * удаление статьи не падает на внешних ключах и уносит зависимые строки.
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete, select

from src.domain.article import ArticleDomain
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    ArticleInteraction,
    ArticleReference,
    Issue,
    Journal,
    Publisher,
    SavedArticle,
)
from src.schemas.article import ArticleCreate, ArticleUpdate

GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"
results: list[bool] = []


def check(name: str, got, want):
    ok = got == want
    results.append(ok)
    tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{tag}] {name}: got={got!r} want={want!r}")


def check_true(name: str, got):
    check(name, bool(got), True)


async def _sweep(db):
    ids = (
        await db.execute(select(Article.id).where(Article.title.like("PART %")))
    ).scalars().all()
    for stmt in (
        delete(ArticleAuthor).where(ArticleAuthor.article_id.in_(ids)),
        delete(ArticleReference).where(ArticleReference.article_id.in_(ids)),
        delete(SavedArticle).where(SavedArticle.article_id.in_(ids)),
        delete(ArticleInteraction).where(ArticleInteraction.article_id.in_(ids)),
        delete(Article).where(Article.id.in_(ids)),
        delete(Issue).where(Issue.title.like("PART %")),
        delete(Journal).where(Journal.name.like("PART %")),
        delete(Publisher).where(Publisher.name.like("PART %")),
    ):
        await db.execute(stmt)
    await db.commit()


async def main() -> int:
    tag = uuid.uuid4().hex[:8]
    dom = ArticleDomain()

    async with AsyncSessionLocal() as db:
        await _sweep(db)

        journal = Journal(name=f"PART J {tag}", slug=f"part-j-{tag}")
        publisher = Publisher(slug=f"part-pub-{tag}", name=f"PART Publisher {tag}")
        db.add_all([journal, publisher])
        await db.flush()
        issue = Issue(journal_id=journal.id, title=f"PART issue {tag}", year=2025)
        db.add(issue)
        await db.flush()
        await db.commit()

        # ------------------------------------------------ create
        print("\n--- создание ---")
        created = await dom.create_article(
            db,
            ArticleCreate(
                title=f"PART Монография {tag}",
                publication_type="monograph",
                publisher_id=publisher.id,
                publication_year=2025,
                isbn="978-0-000000-0",
                authors=["Иванов И.", "Петров П."],
                keywords=["алгебра", "топология"],
                metadata={"note": "из формы"},
                published=True,
            ),
        )
        check("тип публикации сохранён", created.publication_type, "monograph")
        check("издатель сохранён", created.publisher_id, publisher.id)
        check("год сохранён", created.publication_year, 2025)
        check("isbn сохранён", created.isbn, "978-0-000000-0")
        check("metadata сохранена", created.meta, {"note": "из формы"})
        check("published сохранён", created.published, True)
        check("список авторов схлопнут в строку", created.authors, "Иванов И., Петров П.")
        check("keywords схлопнуты", created.keywords, "алгебра, топология")
        check_true("slug сгенерирован", created.slug)

        # ------------------------------------------------ update
        print("\n--- обновление (exclude_unset) ---")
        updated = await dom.update_article(
            db, created.id, ArticleUpdate(title=f"PART Монография 2 {tag}")
        )
        assert updated is not None
        check("заголовок обновлён", updated.title, f"PART Монография 2 {tag}")
        check("тип не затёрт", updated.publication_type, "monograph")
        check("издатель не затёрт", updated.publisher_id, publisher.id)
        check("isbn не затёрт", updated.isbn, "978-0-000000-0")
        check("metadata не затёрта", updated.meta, {"note": "из формы"})

        cleared = await dom.update_article(db, created.id, ArticleUpdate(isbn=None))
        assert cleared is not None
        # Явный null отличается от «поля не было» — это и есть смысл exclude_unset.
        check("явный null очищает поле", cleared.isbn, None)
        check("соседнее поле при этом цело", cleared.publication_year, 2025)

        # ------------------------------------------------ фильтры
        print("\n--- фильтры и пагинация ---")
        a2 = await dom.create_article(
            db,
            ArticleCreate(
                title=f"PART Статья {tag}", issue_id=issue.id, publication_type="article"
            ),
        )
        db.add_all([
            ArticleInteraction(article_id=a2.id, view=1),
            ArticleInteraction(article_id=a2.id, view=1),
            ArticleInteraction(article_id=a2.id, download=1),
        ])
        await db.commit()

        items, total = await dom.list_articles(db, journal_id=journal.id)
        check("фильтр по журналу через выпуски", total, 1)
        check("вернулась статья выпуска", items[0]["id"], a2.id)
        check("имя журнала доклеено", items[0]["journal_name"], f"PART J {tag}")

        items, total = await dom.list_articles(db, publisher_id=publisher.id)
        check("фильтр по издателю", total, 1)
        check("имя издателя доклеено", items[0]["publisher_name"], f"PART Publisher {tag}")

        items, _ = await dom.list_articles(db, issue_id=[issue.id], with_stats=True)
        check("просмотры посчитаны", items[0]["views"], 2)
        check("скачивания посчитаны", items[0]["downloads"], 1)

        items, _ = await dom.list_articles(db, journal_id=journal.id)
        check_true("тяжёлые поля не выгружаются", "embedding" not in items[0])
        check_true("tsvector не выгружается", "search_vector" not in items[0])

        cnt = await dom.count_articles(
            db,
            issue_id=None,
            journal_id=journal.id,
            publisher_id=None,
            admin_id=None,
            section_id=None,
            publication_type=None,
            field_of_science=None,
            published=None,
            has_doi=None,
        )
        check("count совпадает с total списка", cnt, 1)

        _, t_page = await dom.list_articles(db, publisher_id=publisher.id, limit=1)
        check("total не зависит от limit", t_page, 1)

        # ------------------------------------------------ удаление
        print("\n--- удаление со связями ---")
        db.add_all([
            ArticleAuthor(article_id=a2.id, author_name="Иванов", author_order=0),
            ArticleReference(article_id=a2.id, cited_article_id=created.id, position=0),
        ])
        await db.commit()

        ok = await dom.delete_article(db, a2.id)
        check("статья со связями удалена", ok, True)
        check("сама статья пропала", await dom.get_article_by_id(db, a2.id), None)
        left_auth = (
            await db.execute(
                select(ArticleAuthor).where(ArticleAuthor.article_id == a2.id)
            )
        ).scalars().all()
        check("авторы удалены вместе со статьёй", len(left_auth), 0)
        # Цитируемая статья должна выжить — уносить чужой контент нельзя.
        check_true("процитированная статья жива", await dom.get_article_by_id(db, created.id))

        check("удаление несуществующей — False", await dom.delete_article(db, 10**9), False)

        # ------------------------------------------------ уборка
        print("\n--- уборка ---")
        await _sweep(db)
        left = (
            await db.execute(select(Article).where(Article.title.like("PART %")))
        ).scalars().all()
        check("остатков фикстур не осталось", len(left), 0)

    total_n, ok_n = len(results), sum(results)
    print(f"\n{'='*46}\nИтог: {ok_n}/{total_n} " + ("— всё зелёное" if ok_n == total_n else "— ЕСТЬ ПАДЕНИЯ"))
    return 0 if ok_n == total_n else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

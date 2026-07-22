"""Проверка модуля новостей.

Что стережём:
  * черновик и отложенная (published_at в будущем) новость невидимы в публичной
    ленте и по slug — неотличимы от 404;
  * публикация без явной даты проставляет published_at; повторный PATCH
    её не сдвигает;
  * фильтр lang и пагинация считают то же, что и total;
  * коллизия slug решается uuid-суффиксом;
  * PATCH с exclude_unset не затирает поля, которых не было в теле;
  * админ-список видит все статусы.
"""
from __future__ import annotations

import asyncio
import datetime
import uuid

from sqlalchemy import delete, select

from src.domain.news import NewsDomain
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import NewsPost
from src.schemas.news import NewsCreate, NewsUpdate

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
    await db.execute(delete(NewsPost).where(NewsPost.title.like("VNEWS %")))
    await db.commit()


async def main() -> int:
    tag = uuid.uuid4().hex[:8]
    dom = NewsDomain()

    async with AsyncSessionLocal() as db:
        await _sweep(db)

        # ------------------------------------------------ черновик невидим
        print("\n--- черновик ---")
        draft = await dom.create_news(
            db,
            NewsCreate(title=f"VNEWS черновик {tag}", body_html="<p>т</p>", lang="ru"),
            admin_id=None,
        )
        check("статус по умолчанию draft", draft.status, "draft")
        check("published_at пуст у черновика", draft.published_at, None)
        check_true("slug сгенерирован", draft.slug)
        items, total = await dom.list_news(db, limit=50)
        check("черновика нет в публичной ленте",
              any(i["id"] == draft.id for i in items), False)
        check("черновик по slug публично не находится",
              await dom.get_published_by_slug(db, draft.slug), None)

        # ------------------------------------------------ публикация
        print("\n--- публикация ---")
        published = await dom.update_news(db, draft.id, NewsUpdate(status="published"))
        assert published is not None
        check_true("published_at проставлен при публикации", published.published_at)
        first_published_at = published.published_at
        found = await dom.get_published_by_slug(db, draft.slug)
        check_true("опубликованная находится по slug", found)
        items, total = await dom.list_news(db, limit=50)
        check_true("опубликованная есть в ленте",
                   any(i["id"] == draft.id for i in items))

        touched = await dom.update_news(
            db, draft.id, NewsUpdate(excerpt="VNEWS тизер")
        )
        check("повторный PATCH не сдвигает published_at",
              touched.published_at, first_published_at)
        check("PATCH не затёр title", touched.title, f"VNEWS черновик {tag}")
        check("PATCH не затёр body_html", touched.body_html, "<p>т</p>")

        # ------------------------------------------------ отложенная
        print("\n--- отложенная публикация ---")
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        scheduled = await dom.create_news(
            db,
            NewsCreate(
                title=f"VNEWS отложенная {tag}",
                status="published",
                published_at=future,
                lang="uz",
            ),
            admin_id=None,
        )
        check("явный published_at не перезаписан",
              scheduled.published_at.replace(tzinfo=datetime.timezone.utc)
              if scheduled.published_at.tzinfo is None else scheduled.published_at,
              future)
        items, _ = await dom.list_news(db, limit=50)
        check("отложенной нет в публичной ленте",
              any(i["id"] == scheduled.id for i in items), False)
        check("отложенная по slug публично не находится",
              await dom.get_published_by_slug(db, scheduled.slug), None)

        # ------------------------------------------------ lang-фильтр и пагинация
        print("\n--- фильтр lang и пагинация ---")
        en = await dom.create_news(
            db,
            NewsCreate(title=f"VNEWS en {tag}", status="published", lang="en"),
            admin_id=None,
        )
        items_ru, total_ru = await dom.list_news(db, lang="ru", limit=50)
        check_true("ru-фильтр находит ru-новость",
                   any(i["id"] == draft.id for i in items_ru))
        check("ru-фильтр не находит en-новость",
              any(i["id"] == en.id for i in items_ru), False)
        _, total_all = await dom.list_news(db, limit=1)
        page, _ = await dom.list_news(db, limit=1, offset=0)
        check("limit=1 отдаёт одну строку", len(page), 1)
        check_true("total считает всю выборку, не страницу", total_all >= 2)

        # ------------------------------------------------ коллизия slug
        print("\n--- коллизия slug ---")
        dup = await dom.create_news(
            db,
            NewsCreate(title=f"VNEWS dup {tag}", slug=draft.slug),
            admin_id=None,
        )
        check_true("slug получил суффикс при коллизии",
                   dup.slug != draft.slug and dup.slug.startswith(draft.slug))

        # ------------------------------------------------ админ-список
        print("\n--- админ-список ---")
        admin_items, _ = await dom.list_news(db, include_unpublished=True, limit=100)
        ids = {i["id"] for i in admin_items}
        check_true("админ видит опубликованную", draft.id in ids)
        check_true("админ видит отложенную", scheduled.id in ids)
        check_true("админ видит черновик (dup)", dup.id in ids)
        drafts_only, _ = await dom.list_news(
            db, include_unpublished=True, status="draft", limit=100
        )
        check("фильтр status=draft не отдаёт опубликованные",
              any(i["id"] == draft.id for i in drafts_only), False)

        # ------------------------------------------------ удаление
        print("\n--- удаление ---")
        check("удаление существующей — True", await dom.delete_news(db, dup.id), True)
        check("удаление несуществующей — False", await dom.delete_news(db, 10**9), False)

        # ------------------------------------------------ уборка
        print("\n--- уборка ---")
        await _sweep(db)
        left = (
            await db.execute(select(NewsPost).where(NewsPost.title.like("VNEWS %")))
        ).scalars().all()
        check("остатков фикстур не осталось", len(left), 0)

    total_n, ok_n = len(results), sum(results)
    print(f"\n{'='*46}\nИтог: {ok_n}/{total_n} " + ("— всё зелёное" if ok_n == total_n else "— ЕСТЬ ПАДЕНИЯ"))
    return 0 if ok_n == total_n else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

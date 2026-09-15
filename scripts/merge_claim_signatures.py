"""Разовая чистка: строка кабинета «Это я» → привязка к подписи из статьи.

До появления `article_authors.from_claim` кнопка «Это я» дописывала к статье
ВТОРУЮ строку авторства с именем из профиля. В результате один человек стоял
под статьёй дважды («F.N. Yadgarov» из самой статьи и «Fayzullo Yadgarov» из
кабинета), попадал к себе же в соавторы, а `scripts/backfill_authors.py`
заводил ему из второго написания вторую карточку автора.

Скрипт приводит накопившиеся данные к новому виду:

1. для каждой строки кабинета ищет в той же статье ничью подпись, похожую на
   имя человека (`may_be_same_person`); если такая РОВНО ОДНА — переносит на
   неё привязку (profile_id/orcid) и удаляет строку кабинета;
2. если подписи нет (человека забыли в метаданных статьи) — строка остаётся,
   но помечается `from_claim = true`;
3. карточку автора, под которой после этого не осталось ни одной подписи,
   отдаёт той карточке, куда переехали работы: переносит на неё profile_id и
   orcid, а пустую удаляет.

По умолчанию НИЧЕГО не пишет — только показывает план. Запуск:

    PYTHONPATH=. .venv/bin/python scripts/merge_claim_signatures.py
    PYTHONPATH=. .venv/bin/python scripts/merge_claim_signatures.py --apply

После `--apply` имеет смысл прогнать `scripts/backfill_authors.py`: он
пересчитает works_count и display_name карточек.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict

from sqlalchemy import delete, func, select, update

from src.domain.author_names import clean, may_be_same_person, split_authors
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    Author,
    AuthorClaim,
    Profile,
)


async def plan(db) -> tuple[list, list, list]:
    """Что менять: (привязать, пометить, слить карточки). Без записи."""
    profiles = {
        p.id: p
        for p in (await db.execute(select(Profile))).scalars().all()
    }
    rows = list((await db.execute(select(ArticleAuthor))).scalars().all())
    by_article: dict[int, list[ArticleAuthor]] = defaultdict(list)
    for r in rows:
        by_article[r.article_id].append(r)

    articles = {
        a.id: a
        for a in (
            await db.execute(select(Article.id, Article.title, Article.authors))
        ).all()
    }
    titles = {aid: (a.title or "") for aid, a in articles.items()}
    # Список авторов самой статьи — источник истины о том, кто под ней стоит.
    signed: dict[int, set[str]] = {
        aid: {clean(n).lower() for n in split_authors(a.authors or "") if clean(n)}
        for aid, a in articles.items()
    }

    bind: list[tuple[ArticleAuthor, ArticleAuthor, str]] = []
    mark: list[ArticleAuthor] = []
    for row in rows:
        if row.profile_id is None or row.from_claim:
            continue
        if profiles.get(row.profile_id) is None:
            continue
        # Строка кабинета — та, которой НЕТ в списке авторов статьи. Сравнение
        # строгое, а не через may_be_same_person: «Fayzullo Yadgarov» и
        # «F.N. Yadgarov» — один человек, но в статье он подписан вторым
        # написанием, и первое как раз и есть лишняя строка. А вот «Lyashenko
        # Vyacheslav» в списке статьи стоит — это настоящая подпись, и трогать
        # её нельзя, даже если она слово в слово совпала с именем профиля.
        if clean(row.author_name or "").lower() in signed.get(row.article_id, set()):
            continue

        free = [
            other
            for other in by_article[row.article_id]
            if other.id != row.id
            and other.profile_id is None
            and other.orcid is None
            and not other.from_claim
            and may_be_same_person(row.author_name or "", other.author_name or "")
        ]
        if len(free) == 1:
            bind.append((row, free[0], titles.get(row.article_id) or ""))
        else:
            mark.append(row)

    # Карточки: после удаления строк кабинета часть из них остаётся без работ.
    doomed_ids = {row.id for row, _, _ in bind}
    alive: dict = defaultdict(set)
    for r in rows:
        if r.author_id and r.id not in doomed_ids:
            alive[r.author_id].add(r.article_id)
    # Куда переезжает привязка: карточка, на которую смотрит связанная подпись.
    moved: dict = {}
    for row, signature, _ in bind:
        if row.author_id and signature.author_id and row.author_id != signature.author_id:
            moved[row.author_id] = signature.author_id

    cards = {
        a.id: a for a in (await db.execute(select(Author))).scalars().all()
    }
    # Удаляем только карточки, опустевшие ИЗ-ЗА этой склейки. Карточки, давно
    # лежащие без подписей (работы удалили и перезалили), — не наше дело: их
    # адреса могли разойтись по ссылкам, и чистить их надо отдельно и осознанно.
    merge: list[tuple[Author, Author]] = []
    stale: list[Author] = []
    for card_id, card in cards.items():
        if alive.get(card_id):
            continue
        target = cards.get(moved.get(card_id)) if moved.get(card_id) else None
        if target is not None:
            merge.append((card, target))
        else:
            stale.append(card)
    return bind, mark, merge, stale


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="записать изменения")
    args = ap.parse_args()

    async with AsyncSessionLocal() as db:
        bind, mark, merge, stale = await plan(db)

        print(f"\nПривязать подпись вместо строки кабинета: {len(bind)}")
        for row, signature, title in bind:
            print(
                f"  статья {row.article_id} «{title[:48]}»: "
                f"{row.author_name!r} → {signature.author_name!r}"
            )
        print(f"\nОставить как строку кабинета (подписи в статье нет): {len(mark)}")
        for row in mark:
            print(f"  статья {row.article_id}: {row.author_name!r}")
        print(f"\nСлить карточки: {len(merge)}")
        for card, target in merge:
            print(f"  {card.slug} (works_count={card.works_count}) → {target.slug}")
        if stale:
            print(f"\nДавно пустые карточки (НЕ трогаем, разбирать отдельно): {len(stale)}")
            for card in stale:
                print(f"  {card.slug} (works_count={card.works_count})")

        if not args.apply:
            print("\nЭто план. Записать: --apply")
            return 0

        for row, signature, _ in bind:
            signature.profile_id = row.profile_id
            signature.orcid = signature.orcid or row.orcid
            signature.is_verified = True
        await db.flush()
        if bind:
            await db.execute(
                delete(ArticleAuthor).where(
                    ArticleAuthor.id.in_([row.id for row, _, _ in bind])
                )
            )
        if mark:
            await db.execute(
                update(ArticleAuthor)
                .where(ArticleAuthor.id.in_([row.id for row in mark]))
                .values(from_claim=True)
            )

        removed = 0
        for card, target in merge:
            # Карточка, где лежат работы, получает привязку к профилю от
            # опустевшей — человек остаётся с одной страницей автора.
            target.profile_id = target.profile_id or card.profile_id
            target.orcid = target.orcid or card.orcid
            # Привязку дублируем в подписи — как `decide_claim`: профиль
            # собирает публикации по `article_authors.profile_id`, и без этого
            # карточка показывала бы больше работ, чем профиль их владельца.
            if target.profile_id:
                await db.execute(
                    update(ArticleAuthor)
                    .where(ArticleAuthor.author_id == target.id)
                    .values(profile_id=target.profile_id)
                )
            # Подписи и заявки ссылаются на карточку по FK — переводим их на
            # выжившую, иначе удаление не пройдёт.
            await db.execute(
                update(ArticleAuthor)
                .where(ArticleAuthor.author_id == card.id)
                .values(author_id=target.id)
            )
            await db.execute(
                update(AuthorClaim)
                .where(AuthorClaim.author_id == card.id)
                .values(author_id=target.id)
            )
            await db.delete(card)
            removed += 1

        await db.commit()

        left = (
            await db.execute(
                select(func.count(ArticleAuthor.id)).where(
                    ArticleAuthor.from_claim.is_(True)
                )
            )
        ).scalar_one()
        print(
            f"\nГотово: связано {len(bind)}, помечено {len(mark)}, "
            f"карточек удалено {removed}; строк кабинета осталось {left}."
        )
        print("Дальше: PYTHONPATH=. .venv/bin/python scripts/backfill_authors.py")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

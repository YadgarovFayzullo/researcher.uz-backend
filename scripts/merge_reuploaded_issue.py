"""Склеить удалённый и перезалитый заново выпуск: старые статьи — обратно в
выпуск, новые копии — удалить.

Откуда берётся: до запрета удаления непустого выпуска его статьи откреплялись
(`issue_id = NULL`) и оставались опубликованными. Редактор Inter Education &
Global Study удалил т. 4 №7, завёл его заново (выпуск 144) и перезалил те же
статьи — на сайте и в карточках авторов каждая работа появилась дважды.

Оставляем СТАРЫЕ записи: у них DOI Zenodo, накопленные просмотры и адреса,
которые уже в индексе. Пара ищется по названию без знаков препинания и
регистра; неоднозначное совпадение не трогаем, а только показываем. Пары, где
название в копии переписано (например, переведено), задаются руками `--pair`.
Сироту, которой в новом выпуске замены нет, `--draft` возвращает в выпуск
черновиком — редактор решит сам.

    python scripts/merge_reuploaded_issue.py --issue 144 --pair 2530:6906 --draft 2526
    python scripts/merge_reuploaded_issue.py --issue 144 --pair 2530:6906 --draft 2526 --apply

Без `--apply` ничего не пишет. В конце печатает JSON со слагами для сброса
ISR-кэша (`POST /api/revalidate` на фронте).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, or_, select, text, update  # noqa: E402

from src.domain.article import ArticleDomain  # noqa: E402
from src.domain.demo import article_is_not_demo  # noqa: E402
from src.infrastructure.persistence.db import AsyncSessionLocal  # noqa: E402
from src.infrastructure.persistence.models import (  # noqa: E402
    Article,
    ArticleAuthor,
    Author,
    Issue,
    Journal,
)

_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)


def title_key(title: str | None) -> str:
    return _NON_ALNUM.sub("", (title or "").lower())


def pair_arg(value: str) -> tuple[int, int]:
    old, new = value.split(":")
    return int(old), int(new)


async def main(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as db:
        issue = (
            await db.execute(select(Issue).where(Issue.id == args.issue))
        ).scalars().first()
        if not issue:
            print(f"Выпуск {args.issue} не найден")
            return 1
        journal = (
            await db.execute(select(Journal).where(Journal.id == issue.journal_id))
        ).scalars().first()

        orphans = (
            await db.execute(
                select(Article).where(
                    Article.issue_id.is_(None),
                    or_(
                        Article.publication_type.is_(None),
                        Article.publication_type == "article",
                    ),
                )
            )
        ).scalars().all()
        copies = (
            await db.execute(select(Article).where(Article.issue_id == issue.id))
        ).scalars().all()
        orphan_by_id = {a.id: a for a in orphans}
        copy_by_id = {a.id: a for a in copies}

        copies_by_key: dict[str, list[Article]] = defaultdict(list)
        for c in copies:
            copies_by_key[title_key(c.title)].append(c)

        pairs: list[tuple[Article, Article]] = []
        ambiguous: list[Article] = []
        unmatched: list[Article] = []
        manual_old = {old for old, _ in args.pair}
        for o in orphans:
            if o.id in manual_old or o.id in args.draft:
                continue
            found = copies_by_key.get(title_key(o.title), [])
            if len(found) == 1:
                pairs.append((o, found[0]))
            elif len(found) > 1:
                ambiguous.append(o)
            else:
                unmatched.append(o)
        for old_id, new_id in args.pair:
            if old_id not in orphan_by_id or new_id not in copy_by_id:
                print(f"--pair {old_id}:{new_id}: старая не сирота или новая не в выпуске")
                return 1
            pairs.append((orphan_by_id[old_id], copy_by_id[new_id]))
        drafts = []
        for draft_id in args.draft:
            if draft_id not in orphan_by_id:
                print(f"--draft {draft_id}: статья не сирота")
                return 1
            drafts.append(orphan_by_id[draft_id])

        new_ids = [n.id for _, n in pairs]
        if len(set(new_ids)) != len(new_ids):
            print("Одна копия досталась двум сиротам — разберите вручную")
            return 1
        kept = [c for c in copies if c.id not in set(new_ids)]

        print(f"Выпуск {issue.id} ({journal.name if journal else '—'}), статей в нём: {len(copies)}")
        print(f"Пар «старая → удалить копию»: {len(pairs)}")
        for o, n in sorted(pairs, key=lambda p: p[0].id):
            print(f"  {o.id:>5} ← {n.id:<5} {o.pages or '':>9}  {(o.title or '')[:80]}")
        print(f"Вернуть черновиком: {[d.id for d in drafts]}")
        print(f"Копии без пары (остаются в выпуске): {[(c.id, (c.title or '')[:60]) for c in kept]}")
        if ambiguous:
            print(f"Неоднозначные сироты (не трогаем): {[a.id for a in ambiguous]}")
        if unmatched:
            print(f"Сироты без копии в этом выпуске (не трогаем): {len(unmatched)}")
            for a in unmatched:
                print(f"  {a.id:>5} {(a.title or '')[:80]}")

        touched_articles = [o.id for o, _ in pairs] + new_ids + [d.id for d in drafts]
        author_ids = set(
            (
                await db.execute(
                    select(ArticleAuthor.author_id).where(
                        ArticleAuthor.article_id.in_(touched_articles),
                        ArticleAuthor.author_id.isnot(None),
                    )
                )
            ).scalars().all()
        )
        revalidate = {
            "articles": sorted(
                {a.slug for a in [o for o, _ in pairs] + [n for _, n in pairs] + drafts if a.slug}
            ),
            "journals": [journal.slug] if journal and journal.slug else [],
            "authors": [],
        }

        if not args.apply:
            print("\nСухой прогон. Для записи добавьте --apply")
            return 0

        # Всё одной транзакцией: delete_articles коммитит в конце, забирая с
        # собой и переносы. Сбой посередине не оставит статью в двух местах.
        await db.execute(
            update(Article)
            .where(Article.id.in_([o.id for o, _ in pairs]))
            .values(issue_id=issue.id, section_id=None)
        )
        if drafts:
            await db.execute(
                update(Article)
                .where(Article.id.in_([d.id for d in drafts]))
                .values(issue_id=issue.id, section_id=None, published=False)
            )
        deleted = await ArticleDomain().delete_articles(db, new_ids)
        print(f"\nПеренесено в выпуск: {len(pairs)}, черновиком: {len(drafts)}, удалено копий: {deleted}")

        # works_count денормализован (scripts/backfill_authors.py) и считается
        # по видимым работам — пересчитываем у задетых карточек тем же правилом.
        emptied = 0
        for author in (
            await db.execute(select(Author).where(Author.id.in_(author_ids)))
        ).scalars().all():
            works = await db.scalar(
                select(func.count(func.distinct(ArticleAuthor.article_id)))
                .join(Article, Article.id == ArticleAuthor.article_id)
                .where(
                    ArticleAuthor.author_id == author.id,
                    Article.published.is_(True),
                    article_is_not_demo(),
                )
            )
            revalidate["authors"].append(author.slug)
            has_rows = await db.scalar(
                select(func.count())
                .select_from(ArticleAuthor)
                .where(ArticleAuthor.author_id == author.id)
            )
            claims = await db.scalar(
                text("select count(*) from author_claims where author_id = :id"),
                {"id": author.id},
            )
            # Карточка, заведённая только ради удалённой копии, теперь пуста.
            # Не трогаем присвоенные и те, по которым есть заявка.
            if not has_rows and not author.profile_id and not claims:
                await db.delete(author)
                emptied += 1
            else:
                author.works_count = works or 0
        await db.commit()
        print(f"Карточек авторов пересчитано: {len(author_ids) - emptied}, удалено пустых: {emptied}")

        revalidate["authors"] = sorted(set(revalidate["authors"]))
        print("\nREVALIDATE " + json.dumps(revalidate, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--issue", type=int, required=True, help="id перезалитого выпуска")
    parser.add_argument("--pair", type=pair_arg, action="append", default=[], help="OLD:NEW вручную")
    parser.add_argument("--draft", type=int, action="append", default=[], help="сирота → выпуск черновиком")
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    sys.exit(asyncio.run(main(parser.parse_args())))

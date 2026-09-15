"""Привязать карточку автора к профилю от имени владельца.

Штатный путь — человек жмёт «Это я» на странице карточки, владелец одобряет в
`/admin/author-claims`. Скрипт нужен, когда владелец решил вопрос вне интерфейса
(написали в поддержку, человек не нашёл кнопку): он заводит заявку и тут же её
одобряет, то есть проходит ровно тот же код `decide_claim`, а не правит таблицы
руками. Значит, вместе с карточкой проставятся `profile_id` на всех её подписях
и уберутся дубли, заведённые кабинетом.

Привязка — решение о чужих публикациях, поэтому скрипт всегда показывает, что
именно уедет в профиль, и без `--apply` ничего не пишет:

    PYTHONPATH=. .venv/bin/python scripts/attach_author_card.py \\
        --slug choriyeva-a-v --profile "Valida Choriyeva"
    PYTHONPATH=. .venv/bin/python scripts/attach_author_card.py \\
        --slug choriyeva-a-v --profile "Valida Choriyeva" --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import select

from src.domain.authors import AuthorCardDomain, AuthorCardError
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    Author,
    Profile,
)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", required=True, help="слаг карточки: /author/<slug>")
    ap.add_argument("--profile", required=True, help="ФИО, id или ORCID профиля")
    ap.add_argument("--apply", action="store_true", help="записать")
    args = ap.parse_args()

    async with AsyncSessionLocal() as db:
        card = (
            await db.execute(select(Author).where(Author.slug == args.slug))
        ).scalar_one_or_none()
        if card is None:
            print(f"Карточки /author/{args.slug} нет")
            return 1

        needle = args.profile.strip()
        cond = (Profile.full_name == needle) | (Profile.orcid_id == needle)
        try:
            cond = cond | (Profile.id == uuid.UUID(needle))
        except ValueError:
            pass
        profiles = list((await db.execute(select(Profile).where(cond))).scalars().all())
        if len(profiles) != 1:
            print(f"Профиль «{needle}»: найдено {len(profiles)} — уточните id или ORCID")
            for p in profiles:
                print(f"    {p.id} {p.full_name!r} orcid={p.orcid_id}")
            return 1
        profile = profiles[0]

        works = (
            await db.execute(
                select(Article.id, Article.title, Article.published)
                .join(ArticleAuthor, ArticleAuthor.article_id == Article.id)
                .where(ArticleAuthor.author_id == card.id)
                .order_by(Article.id)
            )
        ).all()

        print(f"\nКарточка: /author/{card.slug} — {card.display_name!r}")
        print(f"Профиль:  {profile.id} — {profile.full_name!r} (ORCID {profile.orcid_id})")
        if card.profile_id and card.profile_id != profile.id:
            print("Карточка уже привязана к другому профилю — сначала отвяжите её")
            return 1
        print(f"\nВ профиль уедет работ: {len(works)}")
        for w in works:
            mark = "" if w.published else "  (черновик)"
            print(f"    {w.id}  {(w.title or '')[:64]}{mark}")

        if not args.apply:
            print("\nЭто план. Записать: --apply")
            return 0

        # Решение принимает владелец платформы — его и пишем в аудит заявки,
        # а не самого заявителя.
        owner = (
            await db.execute(select(Profile).where(Profile.role == "owner").limit(1))
        ).scalars().first()
        if owner is None:
            print("В базе нет профиля с ролью owner — некому одобрять")
            return 1

        domain = AuthorCardDomain()
        try:
            claim = await domain.request_claim(
                db, card.slug, str(profile.id), note="привязка владельцем вручную"
            )
            if claim.get("claim_id"):
                await domain.decide_claim(
                    db,
                    claim["claim_id"],
                    approve=True,
                    decided_by=str(owner.id),
                    reason="привязка владельцем вручную",
                )
        except AuthorCardError as e:
            print(f"Отказ: {e}")
            return 1

        linked = (
            await db.execute(
                select(ArticleAuthor.id).where(
                    ArticleAuthor.author_id == card.id,
                    ArticleAuthor.profile_id == profile.id,
                )
            )
        ).scalars().all()
        print(f"\nГотово: карточка привязана, подписей с профилем — {len(linked)}.")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Напоминание тем, кто пришёл из рассылки, зарегистрировался и застрял.

Из первой волны (uz-1a) на платформу пришли девять человек, а заявку «Это я»
подали трое. Остальные завели аккаунт и до кнопки на карточке не дошли — им
уходит одно письмо со ссылкой на их страницу (шаблон notify/claim-reminder).

Само письмо — не лечение, а догон: застревали они из-за того, что кнопка «Это
я» вела на вход, а вход возвращал на главную, теряя `?next` (исправлено в
src/core/redirects.py и на фронте). Эти люди потерялись до починки.

Кого пропускаем:
- адрес в стоп-листе рассылки (отписался, жалоба, недоставка);
- карточка уже присвоена или по ней есть заявка — проверяет и сам
  `send_claim_reminder`, здесь это нужно, чтобы не печатать лишних;
- напоминание на этот адрес уже уходило (campaign='claim-reminder' в журнале
  отправок): второе письмо о том же выглядит как спам.

Без --send ничего не отправляется: печатается, кому ушло бы письмо.

Боевая отправка — ТОЛЬКО на прод-сервере, внутри контейнера api: стоп-лист
отписок и журнал отправок живут в прод-базе, на локальной копии их нет.

    docker exec app-api-1 python scripts/claim_reminder_send.py           # просмотр
    docker exec app-api-1 python scripts/claim_reminder_send.py --send
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from src.core.config import settings  # noqa: E402
from src.domain.notifications import send_claim_reminder  # noqa: E402
from src.infrastructure.persistence.db import AsyncSessionLocal  # noqa: E402
from src.infrastructure.persistence.models import (  # noqa: E402
    Author,
    AuthorClaim,
    OutreachSend,
    OutreachSuppression,
    User,
)

CAMPAIGN = "claim-reminder"
# Лимит Resend по умолчанию — 2 запроса в секунду.
PAUSE_SECONDS = 0.6


async def pick(db) -> tuple[list[dict], dict[str, int]]:
    """Кому имеет смысл напомнить: (список, счётчики пропусков)."""
    rows = (
        await db.execute(
            select(
                OutreachSend.email,
                OutreachSend.author_slug,
                OutreachSend.campaign,
                OutreachSend.created_at,
            )
            .where(OutreachSend.status == "sent")
            .order_by(OutreachSend.created_at)
        )
    ).all()

    offered: dict[str, str] = {}  # адрес → карточка из первого письма
    reminded: set[str] = set()
    for r in rows:
        email = (r.email or "").strip().lower()
        if not email:
            continue
        if r.campaign == CAMPAIGN:
            reminded.add(email)
        elif r.author_slug and email not in offered:
            offered[email] = r.author_slug
    if not offered:
        return [], {}

    suppressed = {
        (e or "").lower()
        for e in (await db.execute(select(OutreachSuppression.email))).scalars()
    }
    claimants = {
        str(p)
        for p in (
            await db.execute(select(AuthorClaim.profile_id).distinct())
        ).scalars()
    }
    users = (
        await db.execute(
            select(User.id, User.email).where(
                func.lower(User.email).in_(list(offered))
            )
        )
    ).all()
    cards = {
        c.slug: c
        for c in (
            await db.execute(
                select(
                    Author.slug, Author.display_name, Author.works_count,
                    Author.profile_id,
                ).where(Author.slug.in_(list(set(offered.values()))))
            )
        ).all()
    }

    picked: list[dict] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for u in users:
        email = (u.email or "").lower()
        slug = offered.get(email)
        card = cards.get(slug) if slug else None
        if email in suppressed:
            skip("адрес в стоп-листе")
        elif email in reminded:
            skip("напоминание уже уходило")
        elif card is None:
            skip("карточки из письма больше нет")
        elif card.profile_id is not None:
            skip("карточка уже присвоена")
        elif str(u.id) in claimants:
            skip("заявка уже подана")
        else:
            picked.append({
                "email": email,
                "slug": slug,
                "card": card.display_name,
                "works": card.works_count,
            })
    return picked, skipped


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--send", action="store_true")
    args = ap.parse_args()

    db_host = make_url(settings.DATABASE_URL).host or ""
    if args.send and db_host in ("localhost", "127.0.0.1", "::1"):
        print(
            "--send на локальной базе запрещён: стоп-лист отписок и журнал "
            "отправок живут в прод-базе. Запускайте в контейнере api на сервере."
        )
        return 1

    async with AsyncSessionLocal() as db:
        picked, skipped = await pick(db)
        batch = picked[: args.limit]

        print(f"Зарегистрировались и не подали заявку: {len(picked)}")
        for reason, n in sorted(skipped.items(), key=lambda x: -x[1]):
            print(f"  пропущено — {reason}: {n}")
        for p in batch:
            print(f"  → {p['email']}: {p['card']} ({p['works']} работ) /{p['slug']}")

        if not args.send:
            print("\nПросмотр. Для отправки добавьте --send.")
            return 0

        sent = failed = 0
        for p in batch:
            ok, info = await send_claim_reminder(p["email"], p["slug"])
            db.add(OutreachSend(
                email=p["email"], campaign=CAMPAIGN, author_slug=p["slug"],
                status="sent" if ok else "failed",
                provider_id=info if ok else None, error=None if ok else info,
            ))
            await db.commit()
            if ok:
                sent += 1
            else:
                failed += 1
                print(f"  ✗ {p['email']}: {info}")
            await asyncio.sleep(PAUSE_SECONDS)
        print(f"\nОтправлено: {sent}, не ушло: {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

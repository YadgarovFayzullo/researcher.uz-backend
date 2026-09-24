"""Напоминание тем, чья заявка «Это я» одобрена, но профиль так и остался пустым.

Письмо claim-approved уже просит указать место работы и ORCID в момент
одобрения, но часть людей на этом останавливается: карточка стала их
профилем, работы на месте, а в профиле — только имя.

Критерий «пустой» — НЕ тот же набор, что считает ProfileCompleteness на
фронте: та шкала засчитывает и avatar_url, а он у входящих через Google
проставляется сам из фото аккаунта (`google_auth.py`, `picture` из userinfo)
ещё ДО того, как человек хоть раз открыл форму редактирования — по нему
пусто/не пусто ничего не говорит. Проверяем поля, которые меняются только
руками или явной привязкой ORCID (`src/domain/orcid.py:link_orcid` пишет
workplace/country/bio/education вместе с orcid_id, поэтому непустой orcid_id
уже покрывает и этот путь): workplace, bio, country, education, orcid_id.

Кого пропускаем:
- адрес в стоп-листе рассылки (отписался, жалоба, недоставка);
- напоминание с этой кампанией уже уходило (второе письмо о том же — спам);
- заявка одобрена недавно (--min-days, по умолчанию 7) — не долбить сразу
  следом за письмом об одобрении;
- в профиле уже заполнено хоть одно из полей — значит, человек в кабинет
  заходил; финальная проверка перед отправкой — внутри send_profile_fill_reminder.

Без --send ничего не отправляется: печатается, кому ушло бы письмо.

Боевая отправка — ТОЛЬКО на прод-сервере, внутри контейнера api: стоп-лист
отписок и журнал отправок живут в прод-базе, на локальной копии их нет.

    docker exec app-api-1 python scripts/profile_fill_reminder.py           # просмотр
    docker exec app-api-1 python scripts/profile_fill_reminder.py --send
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from src.core.config import settings  # noqa: E402
from src.domain.notifications import send_profile_fill_reminder  # noqa: E402
from src.infrastructure.persistence.db import AsyncSessionLocal  # noqa: E402
from src.infrastructure.persistence.models import (  # noqa: E402
    Author,
    AuthorClaim,
    OutreachSend,
    OutreachSuppression,
    Profile,
    User,
)

CAMPAIGN = "profile-fill-reminder"
EMPTY_FIELDS = ("workplace", "bio", "country", "education", "orcid_id")
# Лимит Resend по умолчанию — 2 запроса в секунду.
PAUSE_SECONDS = 0.6


def _blank(value: str | None) -> bool:
    return not (value or "").strip()


async def pick(db, min_days: int) -> tuple[list[dict], dict[str, int]]:
    """Кому имеет смысл напомнить: (список, счётчики пропусков)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=min_days)

    rows = (
        await db.execute(
            select(
                User.id.label("user_id"), User.email,
                Profile.full_name, Profile.workplace, Profile.bio,
                Profile.country, Profile.education, Profile.orcid_id,
                Author.display_name, Author.works_count,
                AuthorClaim.decided_at,
            )
            .select_from(AuthorClaim)
            .join(Author, Author.id == AuthorClaim.author_id)
            .join(Profile, Profile.id == AuthorClaim.profile_id)
            .join(User, User.id == Profile.id)
            .where(AuthorClaim.status == "approved")
        )
    ).all()

    suppressed = {
        (e or "").lower()
        for e in (await db.execute(select(OutreachSuppression.email))).scalars()
    }
    reminded = {
        (e or "").lower()
        for e in (
            await db.execute(
                select(OutreachSend.email).where(OutreachSend.campaign == CAMPAIGN)
            )
        ).scalars()
    }

    picked: list[dict] = []
    skipped: dict[str, int] = {}
    seen_emails: set[str] = set()

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for r in rows:
        email = (r.email or "").strip().lower()
        if not email:
            continue
        if email in suppressed:
            skip("адрес в стоп-листе")
        elif email in reminded:
            skip("напоминание уже уходило")
        # Один человек может иметь несколько одобренных карточек (несколько
        # author_claims на один аккаунт) — письмо шлём одно, не по штуке за карточку.
        elif email in seen_emails:
            skip("уже в этой партии (другая карточка)")
        elif r.decided_at is not None and r.decided_at > cutoff:
            skip("одобрено недавно")
        elif not all(_blank(getattr(r, f)) for f in EMPTY_FIELDS):
            skip("профиль уже частично заполнен")
        else:
            seen_emails.add(email)
            picked.append({
                "email": email,
                "user_id": str(r.user_id),
                "name": r.full_name,
                "author_name": r.display_name,
                "works": r.works_count,
            })
    return picked, skipped


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--min-days", type=int, default=7)
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
        picked, skipped = await pick(db, args.min_days)
        batch = picked[: args.limit]

        print(f"Одобрены, профиль пустой: {len(picked)}")
        for reason, n in sorted(skipped.items(), key=lambda x: -x[1]):
            print(f"  пропущено — {reason}: {n}")
        for p in batch:
            print(f"  → {p['email']}: {p['name'] or p['author_name']} ({p['works']} работ)")

        if not args.send:
            print("\nПросмотр. Для отправки добавьте --send.")
            return 0

        sent = failed = 0
        for p in batch:
            ok, info = await send_profile_fill_reminder(
                p["email"], user_id=p["user_id"], author_name=p["author_name"],
                works=p["works"],
            )
            db.add(OutreachSend(
                email=p["email"], campaign=CAMPAIGN, author_slug=None,
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

"""Рассылка авторам статей волнами (узбекские письма).

Список адресов — CSV из разбора PDF (`emails_ready.csv`). В репозиторий он не
кладётся: это персональные данные. Скрипт берёт из CSV адрес и слаг карточки,
а всё, что могло измениться (число работ, присвоена ли карточка), читает из
базы в момент запуска.

Кого пропускаем всегда:
- адрес в стоп-листе (отписался, жалоба, недоставка);
- адрес уже зарегистрирован на платформе (`users.email`);
- карточка уже присвоена или по ней висит заявка «Это я»;
- карточке уже писали в этой волне на другой адрес — у человека бывает два
  ящика в разных статьях, и два одинаковых письма выглядят как спам.

Без --send ничего не отправляется: печатается, кому ушло бы письмо.

Боевая отправка — ТОЛЬКО на прод-сервере, внутри контейнера api. Причины две:
стоп-лист пополняет ручка отписки в прод-базе, и скрипт на локальной копии
его не видит (отписавшийся получит напоминание); а ссылка отписки подписана
SECRET_KEY, и подпись локального ключа прод отвергнет. Поэтому --send на
localhost скрипт отказывается выполнять.

    docker cp emails_ready.csv app-api-1:/tmp/emails_ready.csv
    docker exec app-api-1 python scripts/outreach_send.py --csv /tmp/emails_ready.csv --campaign uz-1a --min-works 3

    # просмотр первой волны
    PYTHONPATH=. .venv/bin/python scripts/outreach_send.py --csv emails_ready.csv --campaign uz-1a --min-works 3
    # отрендерить письма в файлы
    ... --preview /tmp/outreach
    # одно письмо себе на проверку (в журнал не пишется)
    ... --test-to me@example.com
    # отправить 50 писем
    ... --send --limit 50
    # напоминание тем, кому первое ушло не меньше 6 дней назад
    ... --campaign uz-2 --send --limit 100
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from src.core.config import settings  # noqa: E402
from src.domain import email_check, outreach  # noqa: E402
from src.infrastructure.persistence.db import AsyncSessionLocal  # noqa: E402
from src.infrastructure.persistence.models import (  # noqa: E402
    Article,
    Author,
    AuthorClaim,
    OutreachSend,
    User,
)

FIRST = ("uz-1a", "uz-1b")
RESEND_URL = "https://api.resend.com/emails"
# Лимит Resend по умолчанию — 2 запроса в секунду.
PAUSE_SECONDS = 0.6
# Столько отказов подряд = проблема не в адресах, а в ключе или домене.
MAX_FAILS_IN_A_ROW = 5


def read_csv(path: Path, confidence: set[str]) -> list[dict]:
    rows = []
    with path.open(newline="") as fh:
        for r in csv.DictReader(fh):
            if r.get("kind") and r["kind"] != "личный":
                continue
            if r.get("confidence") not in confidence or not r.get("author_slug"):
                continue
            rows.append(r)
    return rows


def max_year(row: dict) -> int:
    """Год самой свежей статьи с этим адресом (колонка years из CSV)."""
    years = [int(y) for y in re.findall(r"\d{4}", row.get("years") or "")]
    return max(years) if years else 0


async def load_state(db, article_slugs: set[str]) -> dict:
    # В CSV колонка sample_article — слаг статьи. В письмо идёт её название
    # ссылкой, поэтому название берём из базы.
    titles = {
        s: " ".join((t or "").split())
        for s, t in (
            await db.execute(
                select(Article.slug, Article.title).where(Article.slug.in_(article_slugs))
            )
        ).all()
    } if article_slugs else {}
    authors = {
        r.slug: r
        for r in (
            await db.execute(select(Author.slug, Author.works_count, Author.profile_id))
        ).all()
    }
    pending = set(
        (
            await db.execute(
                select(Author.slug)
                .join(AuthorClaim, AuthorClaim.author_id == Author.id)
                .where(AuthorClaim.status == "pending")
            )
        ).scalars()
    )
    users = {
        e.lower()
        for e in (await db.execute(select(User.email).where(User.email.isnot(None)))).scalars()
    }
    sends = (
        await db.execute(
            select(
                OutreachSend.email,
                OutreachSend.campaign,
                OutreachSend.author_slug,
                OutreachSend.created_at,
            ).where(OutreachSend.status == "sent")
        )
    ).all()
    return {
        "authors": authors,
        "pending": pending,
        "users": users,
        "suppressed": await outreach.suppressed_set(db),
        "sends": sends,
        "titles": titles,
    }


async def address_problems(rows: list[dict]) -> dict[str, str]:
    """Адреса, которым слать бессмысленно: битый синтаксис, опечатка в домене,
    служебный ящик, домен без почты. MX проверяется один раз на домен."""
    mx_cache: dict[str, bool | None] = {}
    problems = {}
    for r in rows:
        email = outreach.normalize(r["email"])
        if email not in problems:
            reason = await email_check.check(email, mx_cache)
            if reason:
                problems[email] = reason
    return problems


def pick(rows: list[dict], state: dict, campaign: str, min_works: int, followup_days: int,
         min_year: int = 0, problems: dict[str, str] | None = None):
    sent_pairs = {(s.email, s.campaign) for s in state["sends"]}
    first_slugs = {s.author_slug for s in state["sends"] if s.campaign in FIRST}
    this_slugs = {s.author_slug for s in state["sends"] if s.campaign == campaign}
    cutoff = datetime.now(timezone.utc) - timedelta(days=followup_days)
    first_old_enough = {
        s.email for s in state["sends"] if s.campaign in FIRST and s.created_at <= cutoff
    }

    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    picked, seen_slugs = [], set()
    for r in rows:
        email = outreach.normalize(r["email"])
        slug = r["author_slug"]
        card = state["authors"].get(slug)
        # Первая партия: из адресов со статьями 2023-2024 отскочило 7 из 20,
        # из 2025-2026 — 2 из 30. Старый адрес в статье — чаще заброшенный ящик.
        if min_year and max_year(r) < min_year:
            skip(f"последняя статья раньше {min_year}")
            continue
        if problems and email in problems:
            skip("адрес не прошёл проверку")
            continue
        if email in state["suppressed"]:
            skip("в стоп-листе")
            continue
        if email in state["users"]:
            skip("уже зарегистрирован")
            continue
        if card is None:
            skip("карточки больше нет")
            continue
        if card.profile_id is not None or slug in state["pending"]:
            skip("карточка присвоена или есть заявка")
            continue
        if slug in seen_slugs:
            skip("второй адрес того же автора")
            continue
        if (email, campaign) in sent_pairs or slug in this_slugs:
            skip("уже писали")
            continue
        works = card.works_count
        if campaign == "uz-1a" and works < max(2, min_works):
            skip("мало работ для 1А")
            continue
        if campaign == "uz-1b" and works != 1:
            skip("не одна работа")
            continue
        if campaign in FIRST and slug in first_slugs:
            skip("уже писали")
            continue
        if campaign == "uz-2" and email not in first_old_enough:
            skip("первое письмо не отправлено или слишком свежее")
            continue
        article_slug = (r.get("sample_article") or "").strip()
        title = state["titles"].get(article_slug)
        if not title:
            skip("статья из списка не найдена в базе")
            continue
        seen_slugs.add(slug)
        picked.append(
            {
                "email": email,
                "slug": slug,
                "name": r["author_name"],
                "works": works,
                "article": title,
                "article_slug": article_slug,
                "journal": r.get("journal", ""),
                "articles": int(r.get("articles") or 0),
            }
        )
    picked.sort(key=lambda p: (-p["works"], -p["articles"]))
    return picked, skipped


async def preflight(client: httpx.AsyncClient, sample: dict) -> list[str]:
    """То, без чего письмо вредит: ссылка ведёт на 404 или отписка не работает."""
    problems = []
    page = f"{settings.OUTREACH_SITE_URL.rstrip('/')}/uz/author/{sample['slug']}"
    unsub = outreach.unsubscribe_url(sample["email"])
    for label, url in (("страница автора", page), ("ручка отписки", unsub)):
        try:
            code = (await client.get(url, follow_redirects=True, timeout=20)).status_code
        except httpx.HTTPError as exc:
            code = f"ошибка сети: {exc}"
        if code != 200:
            problems.append(f"{label} {url} → {code}")
    if not settings.RESEND_API_KEY or not settings.OUTREACH_FROM:
        problems.append("не заданы RESEND_API_KEY и/или OUTREACH_FROM")
    return problems


async def send_one(client: httpx.AsyncClient, campaign: str, to: str, letter: dict,
                   idempotency: str | None, unsub_header: bool = True) -> tuple[bool, str]:
    headers = {"Authorization": f"Bearer {settings.RESEND_API_KEY}"}
    if idempotency:
        headers["Idempotency-Key"] = idempotency
    payload = {
        "from": settings.OUTREACH_FROM,
        "to": [to],
        "subject": letter["subject"],
        "html": letter["html"],
        "text": letter["text"],
        "tags": [{"name": "campaign", "value": campaign.replace("-", "_")}],
    }
    # Заголовок — самый явный для Gmail признак рассылки: с ним письмо уходит
    # во вкладку «Оповещения». Без него остаётся ссылка отписки в тексте. Строго
    # обязателен он только от 5000 писем в сутки в Gmail, но без него человек
    # чаще жмёт «Спам» вместо «Отписаться» — отключать осознанно.
    if unsub_header:
        payload["headers"] = {
            "List-Unsubscribe": f"<{letter['unsubscribe']}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
    if settings.OUTREACH_REPLY_TO:
        payload["reply_to"] = settings.OUTREACH_REPLY_TO
    for attempt in range(3):
        resp = await client.post(RESEND_URL, json=payload, headers=headers, timeout=30)
        if resp.status_code == 429:
            await asyncio.sleep(2 ** attempt)
            continue
        if resp.status_code < 300:
            return True, resp.json().get("id", "")
        return False, f"{resp.status_code}: {resp.text[:500]}"
    return False, "429: rate limit"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, type=Path)
    ap.add_argument("--campaign", required=True, choices=outreach.CAMPAIGNS)
    ap.add_argument("--confidence", default="точно",
                    help="через запятую: точно,вероятно")
    ap.add_argument("--min-works", type=int, default=0)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--followup-days", type=int, default=6)
    ap.add_argument("--min-year", type=int, default=0,
                    help="только адреса, у которых последняя статья не раньше этого года")
    ap.add_argument("--preview", type=Path, help="записать письма в эту папку")
    ap.add_argument("--test-to", help="одно письмо на этот адрес, без записи в журнал")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--no-unsub-header", action="store_true",
                    help="не ставить List-Unsubscribe (ссылка в тексте остаётся)")
    args = ap.parse_args()

    db_host = make_url(settings.DATABASE_URL).host or ""
    if args.send and db_host in ("localhost", "127.0.0.1", "::1"):
        print(
            "--send на локальной базе запрещён: стоп-лист отписок живёт в прод-базе, "
            "а ссылки отписки подписываются прод-ключом. Запускайте в контейнере api на сервере."
        )
        return 1

    rows = read_csv(args.csv, set(args.confidence.split(",")))
    async with AsyncSessionLocal() as db:
        state = await load_state(
            db, {(r.get("sample_article") or "").strip() for r in rows} - {""}
        )
        problems = await address_problems(rows)
        picked, skipped = pick(rows, state, args.campaign, args.min_works, args.followup_days,
                               args.min_year, problems)
        if problems:
            print(f"Адресов не прошли проверку: {len(problems)}")
            for email, reason in sorted(problems.items())[:15]:
                print(f"  ✗ {email}: {reason}")
        batch = picked[: args.limit]

        print(f"Кампания {args.campaign}: подходит {len(picked)}, в этой партии {len(batch)}")
        for reason, n in sorted(skipped.items(), key=lambda x: -x[1]):
            print(f"  пропущено — {reason}: {n}")
        for p in batch[:10]:
            letter = outreach.render(args.campaign, p["email"], p)
            print(f"  {p['email']:<40} {p['works']:>3}  {letter['subject']}")
        if len(batch) > 10:
            print(f"  … и ещё {len(batch) - 10}")
        if not batch:
            return 0

        if args.preview:
            args.preview.mkdir(parents=True, exist_ok=True)
            for p in batch:
                letter = outreach.render(args.campaign, p["email"], p)
                (args.preview / f"{p['slug']}.html").write_text(
                    f"<!-- {letter['subject']} -->\n{letter['html']}"
                )
            print(f"Письма записаны в {args.preview}")

        if not (args.send or args.test_to):
            print("Просмотр. Для отправки добавьте --send.")
            return 0

        async with httpx.AsyncClient() as client:
            problems = await preflight(client, batch[0])
            # Тестовое письмо уходит только владельцу: битая ссылка в нём —
            # повод предупредить, а не отказать. Боевую отправку она стопорит.
            fatal = [p for p in problems if "RESEND_API_KEY" in p] if args.test_to else problems
            for p in problems:
                print(f"  {'✗' if p in fatal else '!'} {p}")
            if fatal:
                print("Отправка остановлена.")
                return 1

            if args.test_to:
                p = batch[0]
                letter = outreach.render(args.campaign, p["email"], p)
                letter["subject"] = "[TEST] " + letter["subject"]
                ok, info = await send_one(client, args.campaign, args.test_to, letter, None,
                                          unsub_header=not args.no_unsub_header)
                print(("Тестовое письмо отправлено: " if ok else "Не отправлено: ") + info)
                return 0 if ok else 1

            sent = failed = fails_in_row = 0
            for p in batch:
                letter = outreach.render(args.campaign, p["email"], p)
                key = f"{args.campaign}-{hashlib.sha256(p['email'].encode()).hexdigest()[:40]}"
                ok, info = await send_one(client, args.campaign, p["email"], letter, key,
                                          unsub_header=not args.no_unsub_header)
                db.add(OutreachSend(
                    email=p["email"], campaign=args.campaign, author_slug=p["slug"],
                    subject=letter["subject"], status="sent" if ok else "failed",
                    provider_id=info if ok else None, error=None if ok else info,
                ))
                await db.commit()
                if ok:
                    sent, fails_in_row = sent + 1, 0
                else:
                    failed, fails_in_row = failed + 1, fails_in_row + 1
                    print(f"  ✗ {p['email']}: {info}")
                    if fails_in_row >= MAX_FAILS_IN_A_ROW:
                        print("Отказы подряд — остановка: проверьте ключ и домен.")
                        break
                await asyncio.sleep(PAUSE_SECONDS)

        total = (await db.execute(
            select(func.count()).select_from(OutreachSend).where(
                OutreachSend.campaign == args.campaign, OutreachSend.status == "sent")
        )).scalar()
        print(f"Отправлено {sent}, ошибок {failed}. Всего в кампании {args.campaign}: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

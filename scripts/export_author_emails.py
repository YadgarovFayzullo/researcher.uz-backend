"""Выгрузка адресов авторов из базы в CSV для рассылки.

Раньше список адресов жил отдельным файлом на сервере и после каждого импорта
собирался заново. Теперь почта лежит в `article_authors.email` (её наполняет
`src/domain/author_contacts.py`), и этот скрипт делает из неё тот же CSV, что
читает `outreach_send.py`, — колонки и их смысл совпадают.

Что означают колонки для рассылки:
  * `kind` — «личный» (gmail и другие публичные ящики) или «вузовский». Рассылка
    по умолчанию берёт только личные: у вузовских доменов отказы доходили до 64%.
  * `confidence` — «точно», если адрес встретился ровно у одной карточки автора;
    если один и тот же ящик подписан под разными людьми (общий кафедральный),
    ставим «возможно», и рассылка такие пропускает.
  * `domain` — «ok» или причина, почему домен не принимает почту (проверка MX).

    docker exec app-api-1 python scripts/export_author_emails.py --out /tmp/emails_db.csv
    docker exec app-api-1 python scripts/export_author_emails.py --out /tmp/emails_db.csv --skip-mx
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from src.domain import email_check  # noqa: E402
from src.infrastructure.persistence.db import AsyncSessionLocal  # noqa: E402

# Публичные почтовые службы: адрес в них принадлежит человеку, а не организации.
FREE_MAIL = frozenset({
    "gmail.com", "mail.ru", "yandex.ru", "yandex.com", "inbox.ru", "bk.ru",
    "list.ru", "internet.ru", "mail.com", "outlook.com", "hotmail.com",
    "yahoo.com", "icloud.com", "umail.uz", "proton.me", "protonmail.com",
})

FIELDS = [
    "email", "kind", "domain", "confidence", "author_slug", "author_name",
    "author_works", "claimed", "articles", "years", "journal", "sample_article",
    "all_authors", "article_ids",
]

SQL = text("""
WITH src AS (
  SELECT lower(aa.email) AS email, a.slug, a.display_name, a.works_count,
         a.profile_id IS NOT NULL AS claimed,
         ar.id AS article_id, ar.slug AS article_slug, i.year, j.name AS journal,
         ar.authors AS all_authors
  FROM article_authors aa
  JOIN articles ar ON ar.id = aa.article_id
  JOIN authors a ON a.id = aa.author_id
  LEFT JOIN issues i ON i.id = ar.issue_id
  LEFT JOIN journals j ON j.id = i.journal_id
  WHERE aa.email IS NOT NULL AND aa.from_claim IS FALSE AND ar.published
)
SELECT email, slug, display_name, works_count, claimed,
       count(*) AS articles,
       min(year) AS year_min, max(year) AS year_max,
       (array_agg(article_slug ORDER BY year DESC NULLS LAST, article_id DESC))[1] AS sample_article,
       (array_agg(journal ORDER BY year DESC NULLS LAST, article_id DESC))[1] AS journal,
       (array_agg(all_authors ORDER BY year DESC NULLS LAST, article_id DESC))[1] AS all_authors,
       string_agg(article_id::text, ' ' ORDER BY article_id) AS article_ids
FROM src
GROUP BY email, slug, display_name, works_count, claimed
ORDER BY works_count DESC, email
""")


def years(a, b) -> str:
    if not a and not b:
        return ""
    a, b = a or b, b or a
    return f"{a}" if a == b else f"{a}–{b}"


def authors_line(value) -> str:
    if isinstance(value, (list, tuple)):
        return "; ".join(str(x) for x in value if x)
    return " ".join(str(value or "").split())


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--skip-mx", action="store_true", help="не проверять домены через DNS")
    ap.add_argument("--only-personal", action="store_true",
                    help="оставить только личные ящики (рассылка и так берёт только их)")
    args = ap.parse_args()

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(SQL)).mappings().all()

    # Один ящик под разными карточками — общий адрес кафедры или ошибка разбора.
    per_email = Counter(r["email"] for r in rows)

    mx_cache: dict[str, bool | None] = {}
    out_rows = []
    for r in rows:
        email = r["email"]
        domain = email.partition("@")[2]
        kind = "личный" if domain in FREE_MAIL else "вузовский"
        if args.only_personal and kind != "личный":
            continue
        problem = None if args.skip_mx else await email_check.check(email, mx_cache)
        out_rows.append({
            "email": email,
            "kind": kind,
            "domain": "ok" if not problem else problem,
            "confidence": "точно" if per_email[email] == 1 else "возможно",
            "author_slug": r["slug"],
            "author_name": r["display_name"],
            "author_works": r["works_count"],
            "claimed": "да" if r["claimed"] else "нет",
            "articles": r["articles"],
            "years": years(r["year_min"], r["year_max"]),
            "journal": r["journal"] or "",
            "sample_article": r["sample_article"] or "",
            "all_authors": authors_line(r["all_authors"]),
            "article_ids": r["article_ids"] or "",
        })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(out_rows)
    args.out.chmod(0o600)

    kinds = Counter(r["kind"] for r in out_rows)
    conf = Counter(r["confidence"] for r in out_rows)
    bad = sum(1 for r in out_rows if r["domain"] != "ok")
    print(f"строк: {len(out_rows)}; адресов: {len({r['email'] for r in out_rows})}")
    print(f"  тип: {dict(kinds)}")
    print(f"  уверенность: {dict(conf)}")
    print(f"  домен не принимает почту: {bad}")
    print(f"  карточка уже присвоена: {sum(1 for r in out_rows if r['claimed'] == 'да')}")
    print(f"файл: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Ключи доступа к OAI-PMH: выдать, посмотреть, отозвать.

Эндпоинт /oai закрыт — работать с ним может только клиент из таблицы
`oai_clients`. Ключ показывается ОДИН раз, при создании: в базе лежит лишь
sha256, восстановить его нельзя (потерялся — выдайте новый и отзовите старый).

    PYTHONPATH=. .venv/bin/python scripts/oai_client.py list
    ... create "EBSCO" --fulltext --ip 3.0.0.0/8 --expires 2027-09-01
    ... create "BASE Bielefeld"            # только метаданные, без PDF
    ... create "Демо-партнёр" --journals 12,17
    ... disable 3          # отозвать (строка остаётся — видно, кто и сколько качал)
    ... enable 3
    ... delete 3

Что стоит ограничивать:
  --fulltext   ссылка на PDF в записи. Партнёру по полным текстам (EBSCO) —
               да; метаданным-агрегаторам — нет.
  --journals   список id журналов. Пусто = вся платформа. Ставьте, если
               договорённость касается конкретных изданий.
  --ip         сети харвестера. Сильно снижает цену утечки ключа: он ездит в
               URL и оседает в чужих логах.
  --expires    дата окончания. Договор кончился — доступ кончился сам.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, time, timezone

from sqlalchemy import delete as sql_delete, select, update

from src.core.oai_access import hash_token, new_token
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import OaiClient


def _parse_expires(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.combine(date.fromisoformat(raw), time(23, 59, 59), tzinfo=timezone.utc)


async def cmd_list() -> int:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(OaiClient).order_by(OaiClient.id))).scalars().all()
    if not rows:
        print("Ключей нет — /oai сейчас недоступен никому.")
        return 0
    for c in rows:
        state = "on " if c.enabled else "OFF"
        if c.expires_at and c.expires_at <= datetime.now(timezone.utc):
            state = "EXP"
        journals = ",".join(map(str, c.allowed_journal_ids)) or "все"
        ips = ",".join(c.ip_allowlist) or "любой"
        seen = c.last_seen_at.strftime("%Y-%m-%d %H:%M") if c.last_seen_at else "никогда"
        print(
            f"#{c.id:<4} [{state}] {c.name}\n"
            f"      журналы: {journals}   IP: {ips}   "
            f"полные тексты: {'да' if c.include_fulltext else 'нет'}\n"
            f"      запросов: {c.requests_count}   последний: {seen} "
            f"({c.last_seen_ip or '—'})"
        )
    return 0


async def cmd_create(args) -> int:
    journals = (
        [int(x) for x in args.journals.split(",") if x.strip()] if args.journals else []
    )
    ips = [x.strip() for x in args.ip.split(",") if x.strip()] if args.ip else []
    token = new_token()
    client = OaiClient(
        name=args.name,
        token_hash=hash_token(token),
        include_fulltext=args.fulltext,
        allowed_journal_ids=journals,
        ip_allowlist=ips,
        expires_at=_parse_expires(args.expires),
        notes=args.notes,
    )
    async with AsyncSessionLocal() as db:
        db.add(client)
        await db.commit()
        await db.refresh(client)

    print(f"Клиент #{client.id}: {client.name}")
    print("Ключ (больше не покажется):\n")
    print(f"  {token}\n")
    print("baseURL для партнёра:")
    print(f"  https://api.researcher.uz/oai?key={token}")
    return 0


async def _set_enabled(client_id: int, value: bool) -> int:
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            update(OaiClient).where(OaiClient.id == client_id).values(enabled=value)
        )
        await db.commit()
    if res.rowcount:
        print(f"Клиент #{client_id}: {'включён' if value else 'отозван'}")
        return 0
    print(f"Клиента #{client_id} нет", file=sys.stderr)
    return 1


async def cmd_delete(client_id: int) -> int:
    async with AsyncSessionLocal() as db:
        res = await db.execute(sql_delete(OaiClient).where(OaiClient.id == client_id))
        await db.commit()
    if res.rowcount:
        print(f"Клиент #{client_id} удалён")
        return 0
    print(f"Клиента #{client_id} нет", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="показать клиентов")

    create = sub.add_parser("create", help="выдать ключ")
    create.add_argument("name")
    create.add_argument("--fulltext", action="store_true", help="отдавать ссылку на PDF")
    create.add_argument("--journals", help="id журналов через запятую (пусто = все)")
    create.add_argument("--ip", help="IP/CIDR через запятую (пусто = любой)")
    create.add_argument("--expires", help="YYYY-MM-DD, включительно")
    create.add_argument("--notes", help="пометка: договор, контакт")

    for name, help_text in (("disable", "отозвать"), ("enable", "вернуть доступ")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("id", type=int)

    rm = sub.add_parser("delete", help="удалить строку целиком")
    rm.add_argument("id", type=int)

    args = parser.parse_args()
    if args.cmd == "list":
        return asyncio.run(cmd_list())
    if args.cmd == "create":
        return asyncio.run(cmd_create(args))
    if args.cmd == "disable":
        return asyncio.run(_set_enabled(args.id, False))
    if args.cmd == "enable":
        return asyncio.run(_set_enabled(args.id, True))
    return asyncio.run(cmd_delete(args.id))


if __name__ == "__main__":
    raise SystemExit(main())

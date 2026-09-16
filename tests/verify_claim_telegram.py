"""Заявка «Это я» решается кнопками в Telegram-боте владельца.

Стережём:
  * новая заявка приходит с кнопками «Принять» / «Отказать»;
  * «Принять» одобряет заявку, переписывает сообщение, шлёт письмо и
    сбрасывает кэш фронта; повторное нажатие ничего не решает;
  * «Отказать» только спрашивает причину, заявка остаётся в очереди;
  * ответ на запрос отклоняет заявку ИМЕННО с этой причиной (экранированной);
  * из чужого чата и без секрета вебхука ничего не решается.

Telegram, письма и сброс кэша подменены записью — сеть не трогаем.

Запуск: PYTHONPATH=. .venv/bin/python tests/verify_claim_telegram.py
"""
from __future__ import annotations

import asyncio
import uuid

import httpx
from sqlalchemy import delete, select

from src.core.config import settings
from src.domain import claim_telegram, notifications
from src.domain.authors import AuthorCardDomain
from src.infrastructure.external import telegram
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import Author, AuthorClaim, Profile, User
from src.main import app

GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"
results: list[bool] = []

SECRET = "test-secret"
OWNER_CHAT = "777000"
calls: list[tuple[str, dict]] = []
side: list[tuple[str, str]] = []


def check(name: str, got, want):
    ok = got == want
    results.append(ok)
    tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{tag}] {name}: got={got!r} want={want!r}")


async def fake_call(method: str, payload: dict):
    calls.append((method, payload))
    return {"message_id": 900 + len(calls)} if method == "sendMessage" else True


async def fake_approved(claim_id: str):
    side.append(("approved-mail", claim_id))


async def fake_rejected(claim_id: str):
    side.append(("rejected-mail", claim_id))


async def fake_revalidate(owner_id, slug, profile_id, orcid):
    side.append(("revalidate", slug))


def last(method: str) -> dict:
    return next(p for m, p in reversed(calls) if m == method)


async def status_of(claim_id: str) -> tuple[str, str | None]:
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(AuthorClaim.status, AuthorClaim.decision_reason).where(
                    AuthorClaim.id == uuid.UUID(claim_id)
                )
            )
        ).first()
    return row.status, row.decision_reason


async def main() -> None:
    telegram._call = fake_call
    claim_telegram.send_claim_approved = fake_approved
    claim_telegram.send_claim_rejected = fake_rejected
    claim_telegram.revalidate_frontend = fake_revalidate
    settings.TELEGRAM_WEBHOOK_SECRET = SECRET
    settings.TELEGRAM_OWNER_CHAT_ID = OWNER_CHAT
    settings.TELEGRAM_BOT_TOKEN = settings.TELEGRAM_BOT_TOKEN or "test-token"

    tag = uuid.uuid4().hex[:8]
    user_id = uuid.uuid4()
    slugs = [f"tgclaim-a-{tag}", f"tgclaim-b-{tag}"]
    async with AsyncSessionLocal() as db:
        db.add(User(id=user_id, email=f"tgclaim-{tag}@example.com"))
        await db.flush()
        db.add(Profile(id=user_id, full_name="Test Claimant", role="authenticated"))
        for slug in slugs:
            db.add(Author(name_key=f"key-{slug}", slug=slug, display_name=f"Test {slug}"))
        await db.commit()

    transport = httpx.ASGITransport(app=app)
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}

    def callback(data: str, message_id: int, chat: str = OWNER_CHAT) -> dict:
        return {
            "update_id": 1,
            "callback_query": {
                "id": "cb",
                "data": data,
                "message": {"message_id": message_id, "chat": {"id": int(chat)}, "text": "x"},
            },
        }

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            async with AsyncSessionLocal() as db:
                first = await AuthorCardDomain().request_claim(db, slugs[0], str(user_id), "моя")
                second = await AuthorCardDomain().request_claim(db, slugs[1], str(user_id))
                again = await AuthorCardDomain().request_claim(db, slugs[0], str(user_id))
            claim_a, claim_b = first["claim_id"], second["claim_id"]
            check("новая заявка помечена created", first.get("created"), True)
            check("повторная — без created", again.get("created"), None)

            print("\n--- уведомление ---")
            await notifications.notify_owner_claim(claim_a)
            keyboard = last("sendMessage")["reply_markup"]["inline_keyboard"]
            check("кнопки решения", [b.get("callback_data") for b in keyboard[0]],
                  [f"clok:{claim_a}", f"clno:{claim_a}"])
            check("callback_data в лимите 64 байта",
                  all(len(b["callback_data"].encode()) <= 64 for b in keyboard[0]), True)

            print("\n--- принять ---")
            r = await client.post("/telegram/webhook", json=callback(f"clok:{claim_a}", 100), headers=headers)
            check("вебхук 200", r.status_code, 200)
            check("заявка одобрена", (await status_of(claim_a))[0], "approved")
            edit = last("editMessageText")
            check("сообщение переписано", (edit["message_id"], "Принята" in edit["text"]), (100, True))
            check("кнопки убраны", "reply_markup" in edit, False)
            check("письмо и сброс кэша", side[-2:], [("approved-mail", claim_a), ("revalidate", slugs[0])])
            check("ответ на кнопку", last("answerCallbackQuery")["text"].startswith("✅"), True)

            await client.post("/telegram/webhook", json=callback(f"clok:{claim_a}", 100), headers=headers)
            check("повторное нажатие", last("answerCallbackQuery")["text"], "Заявка уже решена")

            print("\n--- отказать ---")
            await client.post("/telegram/webhook", json=callback(f"clno:{claim_b}", 200), headers=headers)
            prompt = last("sendMessage")
            check("запрос причины с полем ответа", prompt["reply_markup"].get("force_reply"), True)
            check("ссылка на заявку в запросе", f"claim:{claim_b}/200" in prompt["text"], True)
            check("заявка ещё в очереди", (await status_of(claim_b))[0], "pending")

            reply = {
                "update_id": 2,
                "message": {
                    "message_id": 301,
                    "chat": {"id": int(OWNER_CHAT)},
                    "text": "Не тот человек <b>",
                    "reply_to_message": {
                        "message_id": 250,
                        "text": f"✍️ Напишите причину…\n\nclaim:{claim_b}/200",
                    },
                },
            }
            foreign = {**reply, "message": {**reply["message"], "chat": {"id": 1}}}
            await client.post("/telegram/webhook", json=foreign, headers=headers)
            check("ответ из чужого чата игнорируется", (await status_of(claim_b))[0], "pending")

            await client.post("/telegram/webhook", json=reply, headers=headers)
            check("отклонена с причиной", await status_of(claim_b), ("rejected", "Не тот человек <b>"))
            edits = [p for m, p in calls if m == "editMessageText"]
            original = next(p for p in reversed(edits) if p["message_id"] == 200)
            check("исходное сообщение с причиной (экранировано)",
                  "Отклонена. Причина: Не тот человек &lt;b&gt;" in original["text"], True)
            check("запрос причины переписан", any(p["message_id"] == 250 for p in edits), True)
            check("письмо об отказе", side[-1], ("rejected-mail", claim_b))

            print("\n--- доступ ---")
            r = await client.post("/telegram/webhook", json=callback(f"clok:{claim_b}", 200),
                                  headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
            check("без секрета — 403", r.status_code, 403)
            await client.post("/telegram/webhook", json=callback(f"clok:{claim_b}", 200, chat="1"), headers=headers)
            check("чужой чат — нет доступа", last("answerCallbackQuery")["text"], "Нет доступа")
    finally:
        async with AsyncSessionLocal() as db:
            ids = select(Author.id).where(Author.slug.in_(slugs))
            await db.execute(delete(AuthorClaim).where(AuthorClaim.author_id.in_(ids)))
            await db.execute(delete(Author).where(Author.slug.in_(slugs)))
            await db.execute(delete(Profile).where(Profile.id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()

    passed = sum(results)
    print(f"\n{passed}/{len(results)} проверок пройдено")
    raise SystemExit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())

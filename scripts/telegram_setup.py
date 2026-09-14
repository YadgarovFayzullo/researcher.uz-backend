"""Настройка Telegram-бота для уведомлений ИИ-проверки.

Три вещи, которые нужно сделать ровно один раз, и все три — через API, а не
в интерфейсе Telegram:

    # 1. Узнать свой chat_id. Сначала напишите боту в личку любое сообщение
    #    (иначе бот не имеет права вам писать — это правило Telegram, а не наше).
    PYTHONPATH=. .venv/bin/python scripts/telegram_setup.py chat-id

    # 2. Прописать вебхук на прод (секрет придумывается здесь же и кладётся
    #    в .env как TELEGRAM_WEBHOOK_SECRET).
    PYTHONPATH=. .venv/bin/python scripts/telegram_setup.py set-webhook \\
        --url https://api.researcher.uz/telegram/webhook --secret <строка>

    # 3. Проверить, что Telegram доволен адресом и не копит ошибки доставки.
    PYTHONPATH=. .venv/bin/python scripts/telegram_setup.py info

    # Разовая проверка, что сообщения доходят:
    PYTHONPATH=. .venv/bin/python scripts/telegram_setup.py test

Токен берётся из TELEGRAM_BOT_TOKEN (.env), либо флагом --token.

Локальная разработка: вебхук требует публичного https, поэтому на localhost
кнопки работать не будут — сообщения при этом отправляются нормально. Держите
вебхук на проде, а локально проверяйте прогон проверки ручкой
POST /issues/<id>/ai-review.
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

from src.core.config import settings

API_BASE = "https://api.telegram.org"


def call(token: str, method: str, payload: dict | None = None) -> dict:
    # httpx, а не urllib: в корпоративной сети с перехватывающим прокси
    # urllib падает на self-signed сертификате, httpx ходит через certifi.
    response = httpx.post(f"{API_BASE}/bot{token}/{method}", json=payload or {}, timeout=30)
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["chat-id", "set-webhook", "delete-webhook", "info", "test"]
    )
    parser.add_argument("--token", default=settings.TELEGRAM_BOT_TOKEN)
    parser.add_argument("--url", help="публичный адрес вебхука (https)")
    parser.add_argument("--secret", help="TELEGRAM_WEBHOOK_SECRET")
    parser.add_argument("--chat-id", default=settings.TELEGRAM_OWNER_CHAT_ID)
    args = parser.parse_args()

    if not args.token:
        print("Нет токена: задайте TELEGRAM_BOT_TOKEN в .env или флаг --token")
        return 1

    if args.command == "chat-id":
        # getUpdates не работает, пока висит вебхук, — Telegram отдаёт апдейты
        # либо туда, либо сюда. Об этом и говорим, а не молчим про пустой список.
        result = call(args.token, "getUpdates")
        updates = result.get("result") or []
        if not updates:
            print(
                "Апдейтов нет. Напишите боту любое сообщение в личку и повторите.\n"
                "Если вебхук уже установлен — сначала delete-webhook."
            )
            return 1
        for update in updates:
            chat = (update.get("message") or {}).get("chat") or {}
            if chat:
                print(f"chat_id = {chat.get('id')}  ({chat.get('username') or chat.get('first_name')})")
        print("\nПоложите его в .env как TELEGRAM_OWNER_CHAT_ID")
        return 0

    if args.command == "set-webhook":
        if not (args.url and args.secret):
            print("Нужны --url и --secret")
            return 1
        result = call(
            args.token,
            "setWebhook",
            {
                "url": args.url,
                "secret_token": args.secret,
                # Нажатия кнопок и команда /start (ею бот отдаёт кнопку
                # мини-аппа). Остальные типы апдейтов нам не нужны и незачем
                # гонять их через прод.
                "allowed_updates": ["callback_query", "message"],
                # На переустановке вебхука выбрасываем накопившуюся очередь:
                # старые нажатия применять уже поздно.
                "drop_pending_updates": True,
            },
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("\nТеперь пропишите тот же секрет в .env: TELEGRAM_WEBHOOK_SECRET=...")
        return 0

    if args.command == "delete-webhook":
        print(json.dumps(call(args.token, "deleteWebhook"), ensure_ascii=False, indent=2))
        return 0

    if args.command == "info":
        info = call(args.token, "getWebhookInfo")
        print(json.dumps(info, ensure_ascii=False, indent=2))
        pending = (info.get("result") or {}).get("pending_update_count")
        if pending:
            print(f"\nВнимание: {pending} недоставленных апдейтов — вебхук отвечает не 200")
        return 0

    if args.command == "test":
        if not args.chat_id:
            print("Нет chat_id: задайте TELEGRAM_OWNER_CHAT_ID или флаг --chat-id")
            return 1
        result = call(
            args.token,
            "sendMessage",
            {
                "chat_id": args.chat_id,
                "text": "researcher.uz: канал уведомлений ИИ-проверки работает.",
            },
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

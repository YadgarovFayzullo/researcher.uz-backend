"""Проверка подписи Telegram Mini App (initData).

Мини-апп открывается внутри Telegram и получает от него строку `initData` —
данные о пользователе, подписанные ключом, производным от токена бота. Это
единственный способ доказать бэкенду, что страницу открыл именно владелец, а не
кто-то, кому попала ссылка: адрес мини-аппа не секрет, а сессии в WebView
может не быть вовсе.

Алгоритм задан Telegram и повторён здесь дословно:

1. из всех полей, кроме `hash`, собирается строка `key=value`, отсортированная
   по ключу и склеенная переводами строк;
2. секрет — HMAC-SHA256 от токена бота с ключом-константой `WebAppData`;
3. подпись — HMAC-SHA256 этой строки с полученным секретом; она обязана
   совпасть с `hash` из initData.

Сравнение подписей — `compare_digest`: обычное `==` сравнивает побайтово и
выходит на первом различии, что делает подбор по времени теоретически
возможным.

Отдельно проверяется срок: `auth_date` старше суток не принимается, иначе
однажды перехваченный initData работал бы вечно.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

from src.core.config import settings

logger = logging.getLogger(__name__)

MAX_AGE_SECONDS = 24 * 60 * 60


class InitDataError(RuntimeError):
    """initData не прошла проверку — подпись, срок или формат."""


def verify_init_data(init_data: str) -> dict:
    """Разобрать и проверить initData. Возвращает поля, включая `user` (dict)."""
    if not settings.TELEGRAM_BOT_TOKEN:
        raise InitDataError("бот не настроен")
    if not init_data:
        raise InitDataError("пустая initData")

    # strict_parsing=False: Telegram присылает и пустые поля, ронять на них
    # разбор незачем.
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise InitDataError("в initData нет hash")

    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(
        b"WebAppData", settings.TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
    expected = hmac.new(
        secret, check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise InitDataError("подпись не совпала")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        raise InitDataError("некорректный auth_date")
    if auth_date <= 0 or time.time() - auth_date > MAX_AGE_SECONDS:
        raise InitDataError("initData устарела")

    user_raw = pairs.get("user")
    user = {}
    if user_raw:
        try:
            user = json.loads(user_raw)
        except ValueError:
            raise InitDataError("поле user не разбирается")
    if not user.get("id"):
        raise InitDataError("в initData нет пользователя")

    return {**pairs, "user": user}


def is_owner_chat(user_id: int | str) -> bool:
    """Тот ли это человек, которому платформа принадлежит.

    В личной переписке chat_id совпадает с id пользователя, поэтому сверяем с
    той же настройкой, что и для уведомлений: отдельного списка админов бота
    заводить не за чем, панель нужна одному владельцу.
    """
    return bool(
        settings.TELEGRAM_OWNER_CHAT_ID
        and str(user_id) == str(settings.TELEGRAM_OWNER_CHAT_ID)
    )

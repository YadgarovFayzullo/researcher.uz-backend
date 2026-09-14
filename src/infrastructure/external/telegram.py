"""Telegram Bot API — канал уведомлений владельцу.

Зачем именно бот. Решение «публиковать выпуск или оставить закрытым» принимает
владелец, и принимает его в тот момент, когда узнал о проблеме, — а узнаёт он
о ней не сидя в админке. Почта для этого плоха: письмо нельзя нажать, по нему
надо идти в браузер и логиниться. Кнопка под сообщением бота — одно касание.

Своя обёртка на httpx, без python-telegram-bot: нам нужны ровно четыре метода
из API, а библиотека тянет за собой собственный event loop и модель приложения,
которые внутри FastAPI только мешают.

Сетевые ошибки здесь НЕ пробрасываются наверх. Уведомление — побочный эффект
модерации: если Telegram лежит, выпуск всё равно должен остаться погашенным, а
не откатиться из-за таймаута HTTP. Провал уходит в лог и в возвращаемый None.
"""
from __future__ import annotations

import html
import logging
from typing import Any

import httpx

from src.core.config import settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
TIMEOUT = 15.0


def escape(text: str | None) -> str:
    """Экранирование под parse_mode=HTML.

    Заголовки статей приходят от редакторов и регулярно содержат «<» (формулы,
    «<0.05»), от чего Telegram отвергает всё сообщение с 400.
    """
    return html.escape(text or "", quote=False)


async def _call(method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.info("Telegram не настроен — %s пропущен", method)
        return None
    url = f"{API_BASE}/bot{settings.TELEGRAM_BOT_TOKEN}/{method}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(url, json=payload)
        data = response.json()
    except Exception:
        logger.exception("Telegram: запрос %s не прошёл", method)
        return None
    if not data.get("ok"):
        # description от Telegram — единственный способ понять, что не так с
        # разметкой или chat_id; без него отладка вслепую.
        logger.error("Telegram: %s вернул ошибку: %s", method, data.get("description"))
        return None
    return data.get("result")


async def send_message(
    text: str,
    *,
    chat_id: str | None = None,
    buttons: list[list[dict[str, Any]]] | None = None,
) -> int | None:
    """Отправить сообщение владельцу. Возвращает message_id (для правки)."""
    target = chat_id or settings.TELEGRAM_OWNER_CHAT_ID
    if not target:
        logger.info("Telegram: chat_id владельца не задан — сообщение не отправлено")
        return None
    payload: dict[str, Any] = {
        "chat_id": target,
        "text": text,
        "parse_mode": "HTML",
        # Превью ссылок раздуло бы сообщение картинкой обложки журнала.
        "link_preview_options": {"is_disabled": True},
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    result = await _call("sendMessage", payload)
    return result.get("message_id") if result else None


async def send_long(
    text: str,
    *,
    chat_id: str | None = None,
    buttons: list[list[dict[str, Any]]] | None = None,
    max_parts: int = 8,
) -> int | None:
    """Длинный отчёт — несколькими сообщениями подряд.

    У Telegram потолок 4096 символов на сообщение, а в выпуске бывает под сотню
    статей с расхождениями. Резать список и писать «…и ещё 11» плохо: именно
    невидимые одиннадцать и могут оказаться теми, из-за которых снимут журнал.
    Поэтому режем по строкам (разрыв внутри HTML-тега сломал бы разметку) и шлём
    частями, нумеруя их, чтобы в чате было видно, что это одна выдача.

    Кнопки уходят на ПОСЛЕДНЮЮ часть: решение принимают, дочитав до конца.
    Возвращается message_id той части, где кнопки, — по нему их потом гасят.
    """
    parts: list[str] = []
    current: list[str] = []
    length = 0
    # 3900, а не 4096: сверху ещё ляжет строка «(2/3)», а лимит считается по
    # символам UTF-16, которых у кириллицы больше, чем кажется.
    limit = 3900
    for line in text.split("\n"):
        if length + len(line) + 1 > limit and current:
            parts.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        parts.append("\n".join(current))

    if len(parts) > max_parts:
        parts = parts[:max_parts]
        parts[-1] += (
            "\n\n<i>Отчёт обрезан: слишком много расхождений. "
            "Остальное — в админке.</i>"
        )

    last_id: int | None = None
    total = len(parts)
    for index, part in enumerate(parts, start=1):
        header = f"<i>({index}/{total})</i>\n" if total > 1 else ""
        last_id = await send_message(
            header + part,
            chat_id=chat_id,
            # Кнопки — только под последней частью.
            buttons=buttons if index == total else None,
        )
    return last_id


async def edit_message(
    chat_id: str | int,
    message_id: int,
    text: str,
    *,
    buttons: list[list[dict[str, Any]]] | None = None,
) -> None:
    """Переписать отправленное сообщение — так фиксируется принятое решение.

    Кнопки при этом убираются (buttons=None): нажать «опубликовать» второй раз
    через час, забыв о первом нажатии, не должно быть возможно.
    """
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    await _call("editMessageText", payload)


async def answer_callback(callback_id: str, text: str = "") -> None:
    """Погасить «часики» на кнопке. Без ответа Telegram крутит их 30 секунд."""
    await _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

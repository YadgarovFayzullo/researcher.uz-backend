"""Вебхук Telegram — приём решения владельца по кнопке.

Ручка публичная (Telegram не умеет ни JWT, ни cookie), поэтому у неё три
рубежа вместо авторизации:

1. **Секрет вебхука** — Telegram шлёт его в `X-Telegram-Bot-Api-Secret-Token`;
   секрет задаётся при установке вебхука (`scripts/telegram_setup.py`). Без
   совпадения — 403.
2. **Chat id** — команда принимается только из чата владельца, заданного в
   `TELEGRAM_OWNER_CHAT_ID`. Даже угадав адрес и секрет, из чужого чата ничего
   не сделать.
3. **run_id прогона** — кнопка привязана к конкретной проверке, старое
   сообщение в истории чата уже ничего не публикует.

Отвечаем всегда 200: на ошибку Telegram повторяет доставку с нарастающими
паузами, и один кривой апдейт застрял бы в очереди навсегда.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.cookies import set_auth_cookies
from src.core.security import create_access_token, create_refresh_token
from src.core.telegram_auth import InitDataError, is_owner_chat, verify_init_data
from src.domain import ai_review
from src.infrastructure.external import telegram
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Profile, User

router = APIRouter()
logger = logging.getLogger(__name__)

# callback_data кнопок: "<действие>:<issue_id>:<run_id>" (лимит Telegram — 64 байта).
ACTIONS = {"aipub": "publish", "aikeep": "keep_blocked"}


def _webapp_url() -> str:
    base = settings.ADMIN_BASE_URL.rstrip("/")
    return f"{base}/ru/tg/review"


@router.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    if not settings.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Telegram не настроен")
    if x_telegram_bot_api_secret_token != settings.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bad secret token")

    update = await request.json()

    message = update.get("message")
    if message:
        # Единственная команда: показать кнопку панели. Диалога у бота нет —
        # он уведомляет и открывает мини-апп.
        chat_id = str((message.get("chat") or {}).get("id") or "")
        text = (message.get("text") or "").strip().lower()
        if chat_id == str(settings.TELEGRAM_OWNER_CHAT_ID) and text.startswith("/start"):
            await telegram.send_message(
                "Панель проверки выпусков.\n\n"
                "Откройте её кнопкой ниже: там видно журналы и выпуски, можно "
                "запустить проверку и решить судьбу номера.",
                chat_id=chat_id,
                buttons=[[{"text": "🔍 Открыть панель", "web_app": {"url": _webapp_url()}}]],
            )
        return {"ok": True}

    callback = update.get("callback_query")
    if not callback:
        return {"ok": True}

    chat_id = str(((callback.get("message") or {}).get("chat") or {}).get("id") or "")
    if chat_id != str(settings.TELEGRAM_OWNER_CHAT_ID):
        logger.warning("telegram: команда из чужого чата %s — игнор", chat_id)
        await telegram.answer_callback(callback["id"], "Нет доступа")
        return {"ok": True}

    data = callback.get("data") or ""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] not in ACTIONS:
        await telegram.answer_callback(callback["id"], "Непонятная команда")
        return {"ok": True}

    action, raw_issue_id, run_id = parts
    try:
        issue_id = int(raw_issue_id)
    except ValueError:
        await telegram.answer_callback(callback["id"], "Непонятная команда")
        return {"ok": True}

    result = await ai_review.apply_decision(
        db,
        issue_id,
        decision=ACTIONS[action],
        run_id=run_id,
        by=f"telegram:{chat_id}",
    )

    if result["status"] == "ok":
        note = (
            f"✅ Выпуск опубликован ({result['published']} статей)"
            if ACTIONS[action] == "publish"
            else "⛔ Выпуск оставлен закрытым"
        )
    elif result["status"] == "already":
        note = f"Решение уже принято: {result['decision']}"
    else:
        note = result.get("reason") or "Не получилось"

    await telegram.answer_callback(callback["id"], note)

    message = callback.get("message") or {}
    if message.get("message_id"):
        # Переписываем исходное сообщение и убираем кнопки: в истории чата
        # должно быть видно, чем кончилось, и нажать второй раз нельзя.
        original = message.get("text") or ""
        await telegram.edit_message(
            chat_id,
            message["message_id"],
            f"{telegram.escape(original)}\n\n<b>{telegram.escape(note)}</b>",
        )
    return {"ok": True}


class WebAppAuthRequest(BaseModel):
    """`initData` как её отдаёт Telegram — строка запроса с подписью."""

    init_data: str


@router.post("/webapp-auth")
async def webapp_auth(
    body: WebAppAuthRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Впустить владельца в мини-апп по подписи Telegram.

    Мини-апп открывается в WebView, где обычной сессии сайта может не быть, а
    логиниться паролем внутри Telegram — плохой опыт. Вместо этого доверяем
    подписи `initData`: её проверка (src/core/telegram_auth.py) доказывает, что
    страницу открыл конкретный пользователь Telegram, а сверка с
    TELEGRAM_OWNER_CHAT_ID — что это владелец платформы.

    Дальше выдаётся обычная сессия сайта, те же httpOnly-cookie, что и после
    входа паролем: мини-апп ходит в те же ручки тем же клиентом, и отдельной
    системы прав для него не появляется.
    """
    try:
        data = verify_init_data(body.init_data)
    except InitDataError as e:
        # Причину наружу не раскрываем: подпись, срок и чужой аккаунт должны
        # выглядеть для постороннего одинаково.
        logger.warning("telegram webapp: initData отклонена (%s)", e)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")

    tg_user = data["user"]
    if not is_owner_chat(tg_user["id"]):
        logger.warning("telegram webapp: чужой аккаунт %s", tg_user.get("id"))
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")

    # Кого пускаем: владельца платформы. Привязки telegram-аккаунта к строке в
    # `profiles` нет и заводить её ради одного человека незачем — берём
    # единственный owner-профиль.
    owner = (
        await db.execute(select(Profile).where(Profile.role == "owner").limit(2))
    ).scalars().all()
    if not owner:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Owner not found")
    if len(owner) > 1:
        logger.warning("telegram webapp: owner-профилей больше одного, берём первый")
    profile = owner[0]

    user = (
        await db.execute(select(User).where(User.id == profile.id))
    ).scalars().first()
    if user is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Owner not found")

    access = create_access_token(str(user.id), extra={"email": user.email})
    refresh = create_refresh_token(str(user.id))
    set_auth_cookies(response, access, refresh)
    return {
        "status": "ok",
        "access_token": access,
        "user": {"id": str(user.id), "email": user.email, "role": profile.role},
    }

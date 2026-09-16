"""Решение по заявке «Это я» кнопками в Telegram-боте владельца.

«Принять» решает сразу. «Отказать» сначала спрашивает причину: бот шлёт
сообщение с полем ответа, и ответ на него становится причиной. Состояния
диалога нет — id заявки и исходного сообщения зашиты в текст запроса и
читаются из `reply_to_message`, поэтому перезапуск API ничего не теряет.

Решение идёт через тот же `decide_claim`, что и админка: те же проверки,
то же письмо автору. Сброс кэша фронта админка делает из браузера, здесь —
вызовом `/api/revalidate` от имени владельца.
"""
from __future__ import annotations

import logging
import re
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.security import create_access_token
from src.domain.authors import AuthorCardDomain, AuthorCardError
from src.domain.notifications import (
    claim_telegram_message,
    load_claim_row,
    send_claim_approved,
    send_claim_rejected,
)
from src.infrastructure.external import telegram
from src.infrastructure.persistence.models import Profile

logger = logging.getLogger(__name__)

REASON_LIMIT = 1000
REF_RE = re.compile(r"claim:([0-9a-f-]{36})/(\d+)")

domain = AuthorCardDomain()


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


async def owner_profile_id(db: AsyncSession) -> uuid.UUID | None:
    # Owner-аккаунтов несколько (служебные без входа); решившим пишем основной —
    # тот, у которого привязан ORCID.
    return (
        await db.execute(
            select(Profile.id)
            .where(Profile.role == "owner")
            .order_by(Profile.orcid_id.is_(None), Profile.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def revalidate_frontend(owner_id: uuid.UUID, slug: str, profile_id, orcid: str | None) -> None:
    """Сбросить ISR карточки и профиля — иначе человек сутки видит «Это я»."""
    token = create_access_token(str(owner_id))
    body = {
        "authors": [slug],
        "researchers": [str(profile_id)],
        "orcids": [orcid] if orcid else [],
    }
    url = f"{settings.ADMIN_BASE_URL.rstrip('/')}/api/revalidate"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(
                url, json=body, cookies={settings.ACCESS_COOKIE_NAME: token}
            )
        if res.status_code != 200:
            logger.warning("claim bot: revalidate вернул %s", res.status_code)
    except Exception:
        logger.exception("claim bot: revalidate не прошёл")


def _decision_note(err: AuthorCardError) -> str:
    text = str(err)
    if "already decided" in text:
        return "Заявка уже решена"
    if "already claimed" in text:
        return "Карточка уже привязана к другому профилю"
    if "not found" in text:
        return "Заявка не найдена"
    return text


async def _finish(chat_id: str, message_id: int | None, row, note: str) -> None:
    if not message_id or row is None:
        return
    await telegram.edit_message(
        chat_id, message_id, f"{claim_telegram_message(row)}\n\n<b>{telegram.escape(note)}</b>"
    )


async def approve(db: AsyncSession, claim_id: str, chat_id: str, message_id: int | None) -> str:
    claim_id = _uuid(claim_id)
    if claim_id is None:
        return "Непонятная команда"
    owner_id = await owner_profile_id(db)
    if owner_id is None:
        return "Не найден аккаунт владельца"
    try:
        await domain.decide_claim(db, claim_id, approve=True, decided_by=str(owner_id))
    except AuthorCardError as err:
        await db.rollback()
        note = _decision_note(err)
        await _finish(chat_id, message_id, await load_claim_row(db, claim_id), note)
        return note

    row = await load_claim_row(db, claim_id)
    note = "✅ Принята — карточка привязана к профилю"
    await _finish(chat_id, message_id, row, note)
    await send_claim_approved(claim_id)
    await revalidate_frontend(owner_id, row.slug, row.profile_id, row.orcid_id)
    return note


async def ask_reason(db: AsyncSession, claim_id: str, chat_id: str, message_id: int | None) -> str:
    claim_id = _uuid(claim_id)
    if claim_id is None or not message_id:
        return "Непонятная команда"
    row = await load_claim_row(db, claim_id)
    if row is None:
        return "Заявка не найдена"
    if row.status != "pending":
        return "Заявка уже решена"
    await telegram.send_message(
        f"✍️ Напишите причину отказа для <b>{telegram.escape(row.display_name)}</b> "
        "ответом на это сообщение. Её получит заявитель.\n\n"
        f"<code>claim:{claim_id}/{message_id}</code>",
        chat_id=chat_id,
        force_reply_placeholder="Причина отказа",
    )
    return "Напишите причину отказа"


async def reject_from_reply(db: AsyncSession, chat_id: str, message: dict) -> None:
    """Ответ владельца на запрос причины — отклонить заявку с этой причиной."""
    prompt = message.get("reply_to_message") or {}
    match = REF_RE.search(prompt.get("text") or "")
    if not match:
        return
    claim_id, original_id = match.group(1), int(match.group(2))
    reason = (message.get("text") or "").strip()
    if not reason:
        await telegram.send_message(
            "Причина пустая — ответьте на запрос текстом.", chat_id=chat_id
        )
        return
    reason = reason[:REASON_LIMIT]

    owner_id = await owner_profile_id(db)
    if owner_id is None:
        await telegram.send_message("Не найден аккаунт владельца.", chat_id=chat_id)
        return
    try:
        await domain.decide_claim(
            db, claim_id, approve=False, decided_by=str(owner_id), reason=reason
        )
    except AuthorCardError as err:
        await db.rollback()
        await telegram.send_message(telegram.escape(_decision_note(err)), chat_id=chat_id)
        return

    row = await load_claim_row(db, claim_id)
    await _finish(chat_id, original_id, row, f"❌ Отклонена. Причина: {reason}")
    if prompt.get("message_id"):
        await telegram.edit_message(
            chat_id, prompt["message_id"], "✅ Причина принята, заявитель получит письмо."
        )
    await send_claim_rejected(claim_id)

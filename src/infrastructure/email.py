"""Отправка писем через Resend (HTTP API, без SDK).

Возвращает (успех, id письма или текст ошибки) и не бросает исключений:
письмо здесь всегда побочный эффект, и его сбой не должен ронять операцию,
ради которой оно отправлялось.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from src.core.config import settings

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"


async def send_email(
    *,
    to: str,
    subject: str,
    html: str,
    text: str,
    sender: str | None = None,
    reply_to: str | None = None,
    headers: dict[str, str] | None = None,
    tags: dict[str, str] | None = None,
) -> tuple[bool, str]:
    sender = sender or settings.NOTIFY_FROM or settings.OUTREACH_FROM
    if not settings.RESEND_API_KEY or not sender:
        return False, "почта не настроена: нет RESEND_API_KEY или отправителя"

    payload: dict = {
        "from": sender,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    if headers:
        payload["headers"] = headers
    if tags:
        payload["tags"] = [{"name": k, "value": v} for k, v in tags.items()]

    auth = {"Authorization": f"Bearer {settings.RESEND_API_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for attempt in range(3):
                resp = await client.post(RESEND_URL, json=payload, headers=auth)
                if resp.status_code == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                if resp.status_code < 300:
                    return True, resp.json().get("id", "")
                return False, f"{resp.status_code}: {resp.text[:500]}"
    except httpx.HTTPError as exc:
        return False, f"сеть: {exc}"
    return False, "429: rate limit"

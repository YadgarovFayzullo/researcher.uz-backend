"""Отписка от рассылки авторам.

GET показывает страницу с кнопкой, а не отписывает сразу: почтовые сканеры
корпоративных ящиков открывают ссылки из писем сами, и отписка по GET снимала
бы людей, которые ничего не нажимали. POST — и кнопка, и one-click из
заголовка List-Unsubscribe-Post (RFC 8058), который Gmail показывает рядом
с отправителем.
"""
from __future__ import annotations

import html
import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.ratelimit import limiter
from src.domain import outreach
from src.infrastructure.persistence.db import get_db

router = APIRouter()


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>{title}</title></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f6f7f9;margin:0;padding:48px 16px;color:#1f2933">
<div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;padding:32px 28px">
<h1 style="font-size:20px;margin:0 0 12px">{title}</h1>{body}</div></body></html>""",
        status_code=status_code,
    )


_INVALID = (
    "Havola yaroqsiz",
    '<p style="margin:0">Havola buzilgan yoki eskirgan. Obunani bekor qilish uchun '
    "xatga javob yozing — manzilingizni qo'lda o'chiramiz.</p>",
    400,
)


@router.get("/unsubscribe", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def unsubscribe_page(request: Request, e: str = "", t: str = ""):
    email = outreach.verify_unsubscribe(e, t)
    if not email:
        return _page(*_INVALID)
    return _page(
        "Obunani bekor qilish",
        f"""<p style="margin:0 0 20px">researcher.uz xatlari endi
<strong>{html.escape(email)}</strong> manziliga yuborilmaydi.</p>
<form method="post"><button type="submit" style="background:#2f6fa7;color:#fff;border:0;
border-radius:8px;padding:12px 22px;font-size:16px;font-weight:600;cursor:pointer">
Obunani bekor qilish</button></form>""",
    )


@router.post("/unsubscribe", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def unsubscribe(
    request: Request, e: str = "", t: str = "", db: AsyncSession = Depends(get_db)
):
    email = outreach.verify_unsubscribe(e, t)
    if not email:
        return _page(*_INVALID)
    await outreach.suppress(db, email, "unsubscribe")
    return _page(
        "Obuna bekor qilindi",
        '<p style="margin:0">Boshqa xat yubormaymiz. Muallif sahifangiz saytda '
        "qoladi — xohlasangiz, istalgan vaqtda uni o'zingizga biriktirishingiz mumkin.</p>",
    )


logger = logging.getLogger(__name__)


def _address(value: str) -> str:
    """`Имя <a@b.c>` или `a@b.c` → `a@b.c`."""
    match = re.search(r"<([^>]+)>", value or "")
    return (match.group(1) if match else value or "").strip()


@router.post("/webhooks/resend")
async def resend_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Отказы и жалобы из Resend — сразу в стоп-лист.

    Раньше адреса отказов переносились в стоп-лист руками со скриншотов панели,
    и следующая партия успевала уйти на них повторно. Постоянный отказ
    (Permanent) означает, что ящика нет, — такой адрес больше не пробуем.
    Временный (Transient: переполнен ящик, сервер недоступен) не блокируем:
    завтра письмо может дойти. Жалоба на спам — стоп навсегда.
    """
    secret = settings.RESEND_WEBHOOK_SECRET
    if not secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Webhook secret is not configured")
    body = await request.body()
    if not outreach.verify_svix(
        secret,
        request.headers.get("svix-id"),
        request.headers.get("svix-timestamp"),
        request.headers.get("svix-signature"),
        body,
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid signature")

    event = json.loads(body)
    kind = event.get("type")
    data = event.get("data") or {}
    recipients = data.get("to") or []
    if isinstance(recipients, str):
        recipients = [recipients]

    reason = None
    if kind == "email.bounced":
        bounce_type = str((data.get("bounce") or {}).get("type", "")).lower()
        if bounce_type == "permanent":
            reason = "bounce"
        else:
            logger.info("Непостоянный отказ (%s) для %s — не блокируем", bounce_type, recipients)
    elif kind == "email.complained":
        reason = "complaint"

    if reason:
        for recipient in recipients:
            await outreach.suppress(db, _address(recipient), reason)
        logger.warning("Resend %s: в стоп-лист %s", kind, recipients)
    return {"ok": True, "suppressed": len(recipients) if reason else 0}

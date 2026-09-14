"""Рассылка авторам статей: шаблоны, подпись ссылки отписки, стоп-лист.

Отправляет письма `scripts/outreach_send.py`, отписку принимает
`src/api/v1/outreach.py`. Здесь то, что нужно обоим.

Ссылка отписки подписана HMAC от SECRET_KEY: без подписи любой мог бы
отписать чужой адрес, а хранить токены в базе незачем — адрес и так в ссылке.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import time
from pathlib import Path
from string import Template

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.domain.author_names import unshout
from src.infrastructure.persistence.models import OutreachSuppression

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "outreach"

# Кампании рассылки. Письмо 1 делится по числу работ: страница автора с одной
# статьёй закрыта noindex (authors.MIN_WORKS_FOR_INDEX), и обещать такому
# человеку «профиль, который находит Google» нельзя.
CAMPAIGNS = ("uz-1a", "uz-1b", "uz-2")

# Темы. Вариант выбирается по адресу детерминированно — это и A/B, и
# гарантия, что повторный запуск не сменит тему у того же человека.
SUBJECTS = {
    "uz-1a": (
        "$name, nashrlaringiz bitta sahifaga jamlandi",
        "researcher.uz'da muallif sahifangiz tayyor",
    ),
    "uz-1b": ("$name, «$article_short» maqolangiz researcher.uz'da",),
    "uz-2": ("$name, muallif sahifangiz hali egasiz",),
}


def normalize(email: str) -> str:
    return (email or "").strip().lower()


def _sign(email: str) -> str:
    mac = hmac.new(
        settings.SECRET_KEY.encode(), b"outreach-unsub:" + email.encode(), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def unsubscribe_token(email: str) -> tuple[str, str]:
    """(e, t) для ссылки: адрес в base64url и его подпись."""
    email = normalize(email)
    e = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")
    return e, _sign(email)


def verify_unsubscribe(e: str, t: str) -> str | None:
    """Адрес из ссылки, если подпись верна; иначе None."""
    try:
        email = base64.urlsafe_b64decode(e + "=" * (-len(e) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    email = normalize(email)
    if not email or not hmac.compare_digest(_sign(email), t or ""):
        return None
    return email


# Насколько старым может быть вебхук. Больше — считаем повтором перехваченного
# запроса: подпись у него верная, но отправлен он не сейчас.
SVIX_TOLERANCE_SECONDS = 300


def verify_svix(secret: str | None, msg_id: str | None, timestamp: str | None,
                signature_header: str | None, body: bytes,
                now: float | None = None) -> bool:
    """Подпись вебхука Resend — схема Svix.

    Подписывается строка `<svix-id>.<svix-timestamp>.<сырое тело>` ключом из
    base64 после префикса `whsec_`, HMAC-SHA256 в base64. В `svix-signature`
    несколько подписей через пробел вида `v1,<подпись>` (при ротации секрета
    старая и новая) — достаточно совпадения одной.
    """
    if not (secret and msg_id and timestamp and signature_header):
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - sent_at) > SVIX_TOLERANCE_SECONDS:
        return False
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except ValueError:
        return False
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for part in signature_header.split():
        version, _, signature = part.partition(",")
        if version == "v1" and hmac.compare_digest(signature, expected):
            return True
    return False


def unsubscribe_url(email: str) -> str:
    e, t = unsubscribe_token(email)
    return f"{settings.OUTREACH_API_URL.rstrip('/')}/outreach/unsubscribe?e={e}&t={t}"


def subject_for(campaign: str, email: str, values: dict[str, str]) -> str:
    variants = SUBJECTS[campaign]
    idx = int(hashlib.sha256(normalize(email).encode()).hexdigest(), 16) % len(variants)
    return Template(variants[idx]).substitute(values)


def render(campaign: str, email: str, row: dict[str, str]) -> dict[str, str]:
    """Тема, HTML и текстовая версия письма для одной строки списка.

    Текстовая часть обязательна: письмо только из HTML фильтры Gmail и Mail.ru
    оценивают хуже.
    """
    site = settings.OUTREACH_SITE_URL.rstrip("/")
    # Названия статей в базе бывают с переносами строк — в письме это рвёт фразу.
    article = " ".join((row.get("article") or "").split())
    # Часть названий в базе набрана целиком капсом, а тема письма капсом — прямое
    # правило спам-фильтров Gmail и Mail.ru. Такие приводим к обычному регистру.
    if article.isupper():
        article = article[:1] + article[1:].lower()
    article_slug = (row.get("article_slug") or "").strip()
    values = {
        # Имя капсом в теме — такое же правило спам-фильтров, как и заголовок.
        "name": unshout((row.get("name") or "").strip()),
        "slug": row["slug"],
        "works": str(row.get("works") or ""),
        "article": article,
        "article_short": article if len(article) <= 50 else article[:47].rstrip() + "…",
        # Канонический адрес статьи — /uz/, как везде, где ссылка уходит наружу.
        "article_url": f"{site}/uz/article/{article_slug}" if article_slug
        else f"{site}/uz/author/{row['slug']}",
        "journal": (row.get("journal") or "").strip(),
        # «Fayzullo Yadgarov, researcher.uz»; подпись по умолчанию уже с
        # названием платформы, и дублировать его не нужно.
        "signature": settings.OUTREACH_SIGNER
        if "researcher.uz" in settings.OUTREACH_SIGNER.lower()
        else f"{settings.OUTREACH_SIGNER}, researcher.uz",
        "site": site,
        "unsubscribe": unsubscribe_url(email),
    }
    escaped = {k: html.escape(v, quote=True) for k, v in values.items()}
    html_body = Template((TEMPLATES / f"{campaign}.html").read_text()).substitute(escaped)
    text_body = Template((TEMPLATES / f"{campaign}.txt").read_text()).substitute(values)
    return {
        "subject": subject_for(campaign, email, values),
        "html": html_body,
        "text": text_body,
        "unsubscribe": values["unsubscribe"],
    }


async def suppress(db: AsyncSession, email: str, reason: str) -> None:
    stmt = (
        insert(OutreachSuppression)
        .values(email=normalize(email), reason=reason)
        .on_conflict_do_nothing(index_elements=["email"])
    )
    await db.execute(stmt)
    await db.commit()


async def suppressed_set(db: AsyncSession) -> set[str]:
    return set((await db.execute(select(OutreachSuppression.email))).scalars().all())

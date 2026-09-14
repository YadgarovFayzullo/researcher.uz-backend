"""Письма, которые платформа шлёт в ответ на действие человека.

В отличие от рассылки (src/domain/outreach.py), здесь адрес подтверждён
регистрацией и письмо ожидаемо, поэтому ни стоп-листа, ни ссылки отписки нет.
"""
from __future__ import annotations

import html
import logging
import uuid
from pathlib import Path
from string import Template

from sqlalchemy import select

from src.core.config import settings
from src.domain.author_names import unshout
from src.infrastructure.email import send_email
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import Author, AuthorClaim, Profile, User

logger = logging.getLogger(__name__)

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "notify"

SUBJECTS = {
    "claim-approved": "$name, muallif sahifangiz profilingizga biriktirildi",
    "claim-rejected": "$name, muallif sahifasi bo'yicha arizangiz ko'rib chiqildi",
    "claim-reminder": "$name, muallif sahifangizni profilingizga biriktiring",
}


def _signature() -> str:
    signer = settings.OUTREACH_SIGNER
    return signer if "researcher.uz" in signer.lower() else f"{signer}, researcher.uz"


def claim_values(*, name: str, author_name: str, works: int, slug: str,
                 reason: str | None = None) -> dict[str, str]:
    """Подстановки для писем по заявке. Вынесены, чтобы тестовое письмо
    собиралось ровно тем же путём, что и настоящее."""
    site = settings.OUTREACH_SITE_URL.rstrip("/")
    reason = " ".join((reason or "").split())
    return {
        "name": unshout((name or author_name or "").strip()),
        "author_name": unshout(author_name),
        "works": str(works),
        # Присвоенная карточка сама уводит на канонический адрес профиля, а
        # неприсвоенная показывает кнопку «Это я» для повторной заявки.
        "url": f"{site}/uz/author/{slug}",
        "reason": f"Sabab: {reason}" if reason
        else "Mualliflikni tasdiqlovchi ma'lumot yetarli bo'lmadi.",
        "signature": _signature(),
    }


def render_claim_letter(kind: str, values: dict[str, str]) -> dict[str, str]:
    escaped = {k: html.escape(v, quote=True) for k, v in values.items()}
    return {
        "subject": Template(SUBJECTS[kind]).substitute(values),
        "html": Template((TEMPLATES / f"{kind}.html").read_text()).substitute(escaped),
        "text": Template((TEMPLATES / f"{kind}.txt").read_text()).substitute(values),
    }


async def _send_claim_letter(claim_id: str, kind: str, expected_status: str) -> None:
    """Запускается фоновой задачей после ответа ручки: своя сессия, потому что
    сессия запроса к этому моменту закрыта. Заявку перечитываем — письмо уходит,
    только если решение действительно такое."""
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(
                    AuthorClaim.status,
                    AuthorClaim.decision_reason,
                    Author.slug,
                    Author.display_name,
                    Author.works_count,
                    User.email,
                    Profile.full_name,
                )
                .join(Author, Author.id == AuthorClaim.author_id)
                .join(User, User.id == AuthorClaim.profile_id)
                .outerjoin(Profile, Profile.id == AuthorClaim.profile_id)
                .where(AuthorClaim.id == uuid.UUID(str(claim_id)))
            )
        ).first()

    if row is None or row.status != expected_status or not row.email:
        logger.info("Письмо %s по заявке %s не отправлено: нет адреса или статус не тот", kind, claim_id)
        return

    letter = render_claim_letter(kind, claim_values(
        name=row.full_name, author_name=row.display_name, works=row.works_count,
        slug=row.slug, reason=row.decision_reason,
    ))
    ok, info = await send_email(
        to=row.email, subject=letter["subject"], html=letter["html"], text=letter["text"],
        reply_to=settings.OUTREACH_REPLY_TO, tags={"type": kind.replace("-", "_")},
    )
    if ok:
        logger.info("Письмо %s по заявке %s отправлено: %s", kind, claim_id, info)
    else:
        logger.warning("Письмо %s по заявке %s не ушло: %s", kind, claim_id, info)


async def send_claim_approved(claim_id: str) -> None:
    """Сообщить автору, что карточка стала его профилем."""
    await _send_claim_letter(claim_id, "claim-approved", "approved")


async def send_claim_reminder(email: str, slug: str) -> tuple[bool, str]:
    """Напоминание зарегистрировавшемуся, который не подал заявку «Это я».

    Разовое письмо уже вошедшему человеку: регистрацию из рассылки он сделал, а
    до кнопки на странице автора не дошёл. Пропускает, если карточка уже
    присвоена или заявка по ней появилась, — второе письмо о сделанном раздражает.
    """
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(Author.id, Author.display_name, Author.works_count,
                       Author.profile_id, Profile.full_name, User.id.label("user_id"))
                .select_from(User)
                .outerjoin(Profile, Profile.id == User.id)
                .join(Author, Author.slug == slug)
                .where(User.email.ilike(email))
            )
        ).first()
        if row is None:
            return False, "нет такого пользователя или карточки"
        if row.profile_id is not None:
            return False, "карточка уже присвоена"
        has_claim = (
            await db.execute(
                select(AuthorClaim.id).where(
                    AuthorClaim.profile_id == row.user_id,
                    AuthorClaim.status.in_(("pending", "approved")),
                )
            )
        ).first()
        if has_claim:
            return False, "заявка уже подана"

    letter = render_claim_letter("claim-reminder", claim_values(
        name=row.full_name, author_name=row.display_name, works=row.works_count, slug=slug,
    ))
    return await send_email(
        to=email, subject=letter["subject"], html=letter["html"], text=letter["text"],
        reply_to=settings.OUTREACH_REPLY_TO, tags={"type": "claim_reminder"},
    )


async def send_claim_rejected(claim_id: str) -> None:
    """Сообщить, что заявка отклонена, с причиной и тем, как подать снова.

    Без письма человек видит отказ, только если сам вернётся на страницу, —
    и не узнает, чем подтвердить авторство во второй раз.
    """
    await _send_claim_letter(claim_id, "claim-rejected", "rejected")

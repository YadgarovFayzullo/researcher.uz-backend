"""Онлайн-сессии конференций: расписание комнат и выдача входа.

Видео идёт через форк Orange Meets на Cloudflare Realtime SFU
(meet.researcher.uz). Бэкенд — единственная точка, где решается, КТО входит в
комнату и КЕМ: воркер комнаты своей базы пользователей не имеет и верит
только подписи токена.

Роль в комнате:
  * moderator — owner платформы или journal_admin серии конференций
    (та же проверка, что даёт право писать в сборник);
  * speaker — автор хотя бы одного доклада этого сборника
    (`article_authors.profile_id`);
  * participant — любой вошедший на платформу.
Гостей без аккаунта пока нет: у комнаты нет своей капчи, а чужие люди в
конференции — единственная реальная угроза для такой ссылки.

Окно входа: модератор — всегда; остальные — с MEET_JOIN_BEFORE_MINUTES до
начала и до конца (ends_at, а без него — 6 часов от начала). Отменённая сессия
не пускает никого.
"""
from __future__ import annotations

import datetime as dt
import secrets

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.domain.authz import can_write_issue
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    ConferenceSection,
    ConferenceSession,
    Issue,
    Profile,
)
from src.schemas.meet import (
    ConferenceSessionCreate,
    ConferenceSessionUpdate,
    MeetRole,
)

DEFAULT_SESSION_HOURS = 6


class MeetDisabled(Exception):
    """MEET_BASE_URL / MEET_JWT_SECRET не заданы — комнаты выключены."""


class SessionClosed(Exception):
    """Вход закрыт: отменена или вне окна времени. `.reason` — код для фронта."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def new_room_slug(issue_id: int) -> str:
    """Слаг комнаты: читаемый префикс сборника + случайная часть. Угадать
    нельзя, но и секретом он не является — вход всё равно по токену."""
    return f"c{issue_id}-{secrets.token_hex(5)}"


def session_window(
    session: ConferenceSession,
) -> tuple[dt.datetime, dt.datetime]:
    """(открытие входа, закрытие входа) для не-модераторов."""
    opens = session.starts_at - dt.timedelta(minutes=settings.MEET_JOIN_BEFORE_MINUTES)
    closes = session.ends_at or (
        session.starts_at + dt.timedelta(hours=DEFAULT_SESSION_HOURS)
    )
    return opens, closes


def is_open_for(
    session: ConferenceSession, role: MeetRole, now: dt.datetime | None = None
) -> bool:
    if session.status != "scheduled":
        return False
    if role == "moderator":
        return True
    now = now or dt.datetime.now(dt.timezone.utc)
    opens, closes = session_window(session)
    return opens <= now <= closes


def display_name(profile: Profile) -> str:
    return (profile.full_name or profile.username or "Участник").strip() or "Участник"


def make_meet_token(
    *,
    profile_id,
    name: str,
    room: str,
    role: MeetRole,
    session_id: int,
    title: str | None = None,
) -> tuple[str, dt.datetime]:
    """JWT для воркера комнаты. Формат — контракт с meet/app/utils/meetToken.server.ts."""
    if not settings.MEET_JWT_SECRET:
        raise MeetDisabled()
    now = dt.datetime.now(dt.timezone.utc)
    exp = now + dt.timedelta(hours=settings.MEET_TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": str(profile_id),
        "name": name,
        "room": room,
        "role": role,
        "sid": session_id,
        # Заголовок сессии для шапки комнаты; воркер своей базы не имеет.
        "title": (title or "")[:200],
        "type": "meet",
        "iat": now,
        "exp": exp,
    }
    return jwt.encode(payload, settings.MEET_JWT_SECRET, algorithm="HS256"), exp


def join_url(token: str) -> str:
    if not settings.MEET_BASE_URL:
        raise MeetDisabled()
    return f"{settings.MEET_BASE_URL.rstrip('/')}/join?t={token}"


class MeetDomain:
    async def list_sessions(
        self, db: AsyncSession, issue_id: int
    ) -> list[ConferenceSession]:
        res = await db.execute(
            select(ConferenceSession)
            .where(ConferenceSession.issue_id == issue_id)
            .order_by(ConferenceSession.starts_at, ConferenceSession.id)
        )
        return list(res.scalars().all())

    async def get_session(
        self, db: AsyncSession, session_id: int
    ) -> ConferenceSession | None:
        res = await db.execute(
            select(ConferenceSession).where(ConferenceSession.id == session_id)
        )
        return res.scalars().first()

    async def _check_section(
        self, db: AsyncSession, issue_id: int, section_id: int | None
    ) -> None:
        """Секция обязана принадлежать тому же сборнику, иначе зал одной
        конференции окажется в расписании другой."""
        if section_id is None:
            return
        res = await db.execute(
            select(ConferenceSection.issue_id).where(
                ConferenceSection.id == section_id
            )
        )
        owner = res.scalar_one_or_none()
        if owner != issue_id:
            raise ValueError("section_not_in_issue")

    async def create_session(
        self, db: AsyncSession, data: ConferenceSessionCreate, created_by
    ) -> ConferenceSession:
        await self._check_section(db, data.issue_id, data.section_id)
        session = ConferenceSession(
            **data.model_dump(),
            room=new_room_slug(data.issue_id),
            created_by=created_by,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session

    async def update_session(
        self, db: AsyncSession, session: ConferenceSession, data: ConferenceSessionUpdate
    ) -> ConferenceSession:
        changes = data.model_dump(exclude_unset=True)
        if "section_id" in changes:
            await self._check_section(db, session.issue_id, changes["section_id"])
        for field, value in changes.items():
            setattr(session, field, value)
        await db.commit()
        await db.refresh(session)
        return session

    async def delete_session(self, db: AsyncSession, session: ConferenceSession) -> None:
        await db.delete(session)
        await db.commit()

    async def role_for(
        self, db: AsyncSession, profile: Profile, session: ConferenceSession
    ) -> MeetRole:
        issue = await db.get(Issue, session.issue_id)
        if issue is not None and await can_write_issue(
            db, role=profile.role, user_id=profile.id, journal_id=issue.journal_id
        ):
            return "moderator"
        res = await db.execute(
            select(ArticleAuthor.id)
            .join(Article, Article.id == ArticleAuthor.article_id)
            .where(
                Article.issue_id == session.issue_id,
                ArticleAuthor.profile_id == profile.id,
            )
            .limit(1)
        )
        if res.scalar_one_or_none() is not None:
            return "speaker"
        return "participant"

    async def join(
        self, db: AsyncSession, profile: Profile, session: ConferenceSession
    ) -> tuple[str, MeetRole, dt.datetime]:
        """→ (url, role, expires_at). SessionClosed / MeetDisabled наверх."""
        role = await self.role_for(db, profile, session)
        if session.status != "scheduled":
            raise SessionClosed("cancelled")
        if not is_open_for(session, role):
            now = dt.datetime.now(dt.timezone.utc)
            opens, _ = session_window(session)
            raise SessionClosed("not_started" if now < opens else "ended")
        token, exp = make_meet_token(
            profile_id=profile.id,
            name=display_name(profile),
            room=session.room,
            role=role,
            session_id=session.id,
            title=session.title,
        )
        return join_url(token), role, exp

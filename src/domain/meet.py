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
import uuid

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.domain.authz import can_write_issue, is_owner
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    ConferenceSection,
    ConferenceSession,
    Issue,
    Journal,
    Profile,
)
from src.infrastructure.storage import public_url, storage
from src.schemas.meet import (
    ConferenceSessionCreate,
    ConferenceSessionUpdate,
    MeetRole,
    StandaloneSessionCreate,
)

DEFAULT_SESSION_HOURS = 6


class MeetDisabled(Exception):
    """MEET_BASE_URL / MEET_JWT_SECRET не заданы — комнаты выключены."""


class SessionClosed(Exception):
    """Вход закрыт: отменена или вне окна времени. `.reason` — код для фронта."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def new_invite_code() -> str:
    return secrets.token_urlsafe(9)[:12].replace("-", "x").replace("_", "y")


def new_room_slug(issue_id: int | None) -> str:
    """Слаг комнаты: читаемый префикс сборника (или m — самостоятельная) +
    случайная часть. Угадать нельзя, но и секретом он не является — вход всё
    равно по токену."""
    prefix = f"c{issue_id}" if issue_id is not None else "m"
    return f"{prefix}-{secrets.token_hex(5)}"


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
    link: str | None = None,
    waiting_room: bool = True,
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
        # Страница сборника с кнопкой «Подключиться» — для «Поделиться» в комнате.
        "link": (link or "")[:300],
        # false — комната пускает участников без допуска организатора.
        "waiting": bool(waiting_room),
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
            invite_code=new_invite_code(),
            created_by=created_by,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session

    async def create_standalone(
        self, db: AsyncSession, data: StandaloneSessionCreate, created_by
    ) -> ConferenceSession:
        session = ConferenceSession(
            **data.model_dump(),
            issue_id=None,
            section_id=None,
            room=new_room_slug(None),
            invite_code=new_invite_code(),
            created_by=created_by,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session

    async def list_mine(
        self, db: AsyncSession, profile: Profile
    ) -> list[ConferenceSession]:
        """Самостоятельные сессии: свои; владелец платформы видит все."""
        q = select(ConferenceSession).where(ConferenceSession.issue_id.is_(None))
        if not is_owner(profile.role):
            q = q.where(ConferenceSession.created_by == profile.id)
        res = await db.execute(q.order_by(ConferenceSession.starts_at.desc()))
        return list(res.scalars().all())

    @staticmethod
    def can_manage_standalone(profile: Profile, session: ConferenceSession) -> bool:
        return is_owner(profile.role) or session.created_by == profile.id

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
        if session.issue_id is None:
            # Самостоятельная сессия: организатор — создатель (и владелец).
            return "moderator" if self.can_manage_standalone(profile, session) else "participant"
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

    @staticmethod
    def invite_url(session: ConferenceSession) -> str:
        """Прямая ссылка на комнату — её и раздаёт организатор."""
        base = (settings.FRONTEND_URL or "https://researcher.uz").rstrip("/")
        return f"{base}/uz/live/{session.invite_code}"

    async def event_link(self, db: AsyncSession, session: ConferenceSession) -> str | None:
        """Ссылка для «Поделиться» в комнате: прямая ссылка на сессию."""
        return self.invite_url(session)

    async def by_invite_code(
        self, db: AsyncSession, code: str
    ) -> ConferenceSession | None:
        if not code or len(code) > 32:
            return None
        res = await db.execute(
            select(ConferenceSession).where(ConferenceSession.invite_code == code)
        )
        return res.scalars().first()

    async def invite_info(self, db: AsyncSession, session: ConferenceSession) -> dict:
        row = None
        if session.issue_id is not None:
            res = await db.execute(
                select(Issue.title, Issue.year, Journal.name, Journal.slug)
                .join(Journal, Journal.id == Issue.journal_id)
                .where(Issue.id == session.issue_id)
            )
            row = res.first()
        return dict(
            id=session.id,
            title=session.title,
            starts_at=session.starts_at,
            ends_at=session.ends_at,
            status=session.status,
            issue_id=session.issue_id,
            event_title=(row[0] or (f"{row[2]} {row[1]}" if row and row[1] else None)) if row else None,
            series_name=row[2] if row else None,
            series_slug=row[3] if row else None,
        )

    def guest_join(
        self, session: ConferenceSession, name: str | None
    ) -> tuple[str, MeetRole, dt.datetime]:
        """Гость без аккаунта — всегда participant, то есть через зал ожидания.
        Имя берём из формы; sub вида guest:<uuid>, чтобы воркер различал людей."""
        if session.status != "scheduled":
            raise SessionClosed("cancelled")
        if not is_open_for(session, "participant"):
            now = dt.datetime.now(dt.timezone.utc)
            opens, _ = session_window(session)
            raise SessionClosed("not_started" if now < opens else "ended")
        clean = " ".join((name or "").split())[:60] or "Гость"
        token, exp = make_meet_token(
            profile_id=f"guest:{uuid.uuid4()}",
            name=clean,
            room=session.room,
            role="participant",
            session_id=session.id,
            title=session.title,
            link=self.invite_url(session),
            waiting_room=session.waiting_room,
        )
        return join_url(token), "participant", exp

    def recording_key(self, session: ConferenceSession, filename: str) -> str:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "webm"
        if not ext.isalnum() or len(ext) > 5:
            ext = "webm"
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"recordings/{session.room}-{stamp}.{ext}"

    async def attach_recording(
        self, db: AsyncSession, session: ConferenceSession, key: str
    ) -> ConferenceSession:
        """Ключ принимаем только из своего префикса и только если объект
        реально лежит в R2 — иначе в карточку можно было бы вписать что угодно."""
        if not key.startswith(f"recordings/{session.room}-"):
            raise ValueError("foreign_key")
        meta = storage.head(key)
        if not meta:
            raise ValueError("object_missing")
        old = session.recording_url
        session.recording_url = public_url(key) or key
        session.recording_size = meta.get("size")
        session.recording_uploaded_at = dt.datetime.now(dt.timezone.utc)
        await db.commit()
        await db.refresh(session)
        if old and old != session.recording_url:
            self._delete_object_by_url(old)
        return session

    async def remove_recording(
        self, db: AsyncSession, session: ConferenceSession
    ) -> ConferenceSession:
        old = session.recording_url
        session.recording_url = None
        session.recording_size = None
        session.recording_uploaded_at = None
        await db.commit()
        await db.refresh(session)
        if old:
            self._delete_object_by_url(old)
        return session

    @staticmethod
    def _delete_object_by_url(url: str) -> None:
        from src.infrastructure.storage import key_from_url

        key = key_from_url(url, default_prefix="recordings")
        if key and key.startswith("recordings/"):
            try:
                storage.delete(key)
            except Exception:
                pass  # файла может уже не быть — карточку это не должно ломать

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
            link=await self.event_link(db, session),
            waiting_room=session.waiting_room,
        )
        return join_url(token), role, exp

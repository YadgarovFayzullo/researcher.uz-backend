"""Онлайн-сессии конференций: роли в комнате, окно входа, токен.

Фикстуры временные (серия конференций, сборник, секция, доклад с автором,
четыре аккаунта), в конце удаляются. Что стережём:
  * роль: owner и journal_admin серии → moderator; автор доклада сборника →
    speaker; остальные → participant; журнал-админ ЧУЖОЙ серии — participant;
  * окно: модератор входит всегда, участник — только с MEET_JOIN_BEFORE_MINUTES
    до начала и до конца; отменённая сессия не пускает никого;
  * секция чужого сборника в сессию не ставится;
  * токен подписан MEET_JWT_SECRET, несёт room/role/name/sid и тип 'meet';
  * без MEET_JWT_SECRET вход отвечает MeetDisabled, расписание работает.

Запуск: DATABASE_URL=... SECRET_KEY=x MEET_JWT_SECRET=s MEET_BASE_URL=https://m \
        PYTHONPATH=. .venv/bin/python tests/verify_meet.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import jwt
from sqlalchemy import delete

from src.core.config import settings
from src.domain.meet import (
    MeetDisabled,
    MeetDomain,
    SessionClosed,
    is_open_for,
    make_meet_token,
)
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    ConferenceSection,
    ConferenceSession,
    Issue,
    Journal,
    JournalAdmin,
    Profile,
    User,
)
from src.schemas.meet import ConferenceSessionCreate, ConferenceSessionUpdate

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results: list[bool] = []


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  [{PASS if ok else FAIL}] {name}: got={got!r} want={want!r}")


async def main():
    assert settings.MEET_JWT_SECRET, "MEET_JWT_SECRET нужен для теста"
    tag = uuid.uuid4().hex[:8]
    domain = MeetDomain()
    now = dt.datetime.now(dt.timezone.utc)

    async with AsyncSessionLocal() as db:
        users: dict[str, User] = {}
        for name, role in [
            ("owner", "owner"),
            ("jadmin", "admin"),
            ("other_admin", "admin"),
            ("speaker", "authenticated"),
            ("plain", "authenticated"),
        ]:
            u = User(id=uuid.uuid4(), email=f"meet-{tag}-{name}@example.invalid")
            db.add(u)
            users[name] = u
        await db.flush()
        profiles: dict[str, Profile] = {}
        for name, u in users.items():
            role = {"owner": "owner", "jadmin": "admin", "other_admin": "admin"}.get(
                name, "authenticated"
            )
            p = Profile(id=u.id, role=role, full_name=f"Meet {name} {tag}")
            db.add(p)
            profiles[name] = p
        await db.flush()

        series = Journal(
            name=f"Meet series {tag}", slug=f"meet-s-{tag}", type="conference_series"
        )
        other = Journal(
            name=f"Meet other {tag}", slug=f"meet-o-{tag}", type="conference_series"
        )
        db.add_all([series, other])
        await db.flush()
        event = Issue(journal_id=series.id, title=f"Meet event {tag}", year=2026)
        other_event = Issue(journal_id=other.id, title=f"Meet other event {tag}")
        db.add_all([event, other_event])
        await db.flush()
        section = ConferenceSection(issue_id=event.id, title="Hall A")
        foreign_section = ConferenceSection(issue_id=other_event.id, title="Hall X")
        db.add_all([section, foreign_section])
        db.add(JournalAdmin(journal_id=series.id, user_id=users["jadmin"].id))
        db.add(JournalAdmin(journal_id=other.id, user_id=users["other_admin"].id))
        await db.flush()
        talk = Article(
            title=f"Meet talk {tag}",
            slug=f"meet-talk-{tag}",
            issue_id=event.id,
            publication_type="conference_paper",
            section_id=section.id,
        )
        db.add(talk)
        await db.flush()
        db.add(
            ArticleAuthor(
                article_id=talk.id,
                profile_id=users["speaker"].id,
                author_order=1,
                author_name="Speaker",
            )
        )
        await db.commit()
        # После rollback ORM-объекты «протухают», а ленивая подгрузка в async-сессии
        # падает MissingGreenlet — id забираем заранее.
        ids = dict(
            events=[event.id, other_event.id],
            talk=talk.id,
            sections=[section.id, foreign_section.id],
            journals=[series.id, other.id],
            users=[u.id for u in users.values()],
        )

        try:
            print("создание:")
            future = await domain.create_session(
                db,
                ConferenceSessionCreate(
                    issue_id=event.id,
                    section_id=section.id,
                    title="Пленарное",
                    starts_at=now + dt.timedelta(hours=3),
                    ends_at=now + dt.timedelta(hours=5),
                ),
                created_by=users["owner"].id,
            )
            check("room slug prefixed by issue", future.room.startswith(f"c{event.id}-"), True)
            check("status default", future.status, "scheduled")
            try:
                await domain.create_session(
                    db,
                    ConferenceSessionCreate(
                        issue_id=event.id,
                        section_id=foreign_section.id,
                        title="Чужая секция",
                        starts_at=now,
                    ),
                    created_by=users["owner"].id,
                )
                check("foreign section rejected", False, True)
            except ValueError as e:
                check("foreign section rejected", str(e), "section_not_in_issue")

            live = await domain.create_session(
                db,
                ConferenceSessionCreate(
                    issue_id=event.id,
                    title="Секция идёт",
                    starts_at=now - dt.timedelta(minutes=10),
                    ends_at=now + dt.timedelta(hours=2),
                ),
                created_by=users["owner"].id,
            )

            print("роли:")
            for name, want in [
                ("owner", "moderator"),
                ("jadmin", "moderator"),
                ("other_admin", "participant"),
                ("speaker", "speaker"),
                ("plain", "participant"),
            ]:
                check(name, await domain.role_for(db, profiles[name], live), want)

            print("окно входа:")
            check("moderator, future", is_open_for(future, "moderator", now), True)
            check("participant, future (3h ahead)", is_open_for(future, "participant", now), False)
            soon = now + dt.timedelta(hours=3) - dt.timedelta(minutes=settings.MEET_JOIN_BEFORE_MINUTES - 1)
            check("participant, 29 min before", is_open_for(future, "participant", soon), True)
            check("participant, live", is_open_for(live, "participant", now), True)
            late = now + dt.timedelta(hours=2, minutes=1)
            check("participant, after end", is_open_for(live, "participant", late), False)

            print("join:")
            url, role, exp = await domain.join(db, profiles["speaker"], live)
            check("url on MEET_BASE_URL", url.startswith(settings.MEET_BASE_URL.rstrip("/") + "/join?t="), True)
            token = url.split("t=", 1)[1]
            payload = jwt.decode(token, settings.MEET_JWT_SECRET, algorithms=["HS256"])
            check("token.room", payload["room"], live.room)
            check("token.role", payload["role"], "speaker")
            check("token.sid", payload["sid"], live.id)
            check("token.type", payload["type"], "meet")
            check("token.title", payload["title"], "Секция идёт")
            check("token.link", payload["link"], f"https://researcher.uz/uz/live/{live.invite_code}")
            print("прямая ссылка:")
            check("invite code generated", len(live.invite_code) >= 10, True)
            found = await domain.by_invite_code(db, live.invite_code)
            check("by_invite_code", found.id if found else None, live.id)
            info = await domain.invite_info(db, live)
            check("invite info series", info["series_slug"], series.slug)
            gurl, grole, _ = domain.guest_join(live, "  Иван   Петров  ")
            gp = jwt.decode(gurl.split("t=", 1)[1], settings.MEET_JWT_SECRET, algorithms=["HS256"])
            check("guest role participant", grole, "participant")
            check("guest name normalized", gp["name"], "Иван Петров")
            check("guest sub prefixed", gp["sub"].startswith("guest:"), True)
            try:
                domain.guest_join(future, None)
                check("guest before window → SessionClosed", None, "not_started")
            except SessionClosed as e:
                check("guest before window → SessionClosed", e.reason, "not_started")
            print("запись:")
            check("recording key prefix", domain.recording_key(live, "x.webm").startswith(f"recordings/{live.room}-"), True)
            check("recording key ext sanitized", domain.recording_key(live, "x.../etc").endswith(".webm"), True)
            try:
                await domain.attach_recording(db, live, "recordings/other-room-1.webm")
                check("foreign key rejected", False, True)
            except ValueError as e:
                check("foreign key rejected", str(e), "foreign_key")
            check("token.name", payload["name"], profiles["speaker"].full_name)
            check("token.sub", payload["sub"], str(users["speaker"].id))
            hours = (exp - now).total_seconds() / 3600
            check("expires in hours", 0 < hours <= settings.MEET_TOKEN_EXPIRE_HOURS + 0.01, True)
            try:
                jwt.decode(token, "wrong-secret", algorithms=["HS256"])
                check("wrong secret rejected", False, True)
            except jwt.InvalidSignatureError:
                check("wrong secret rejected", True, True)

            try:
                await domain.join(db, profiles["plain"], future)
                check("participant before window → SessionClosed", None, "not_started")
            except SessionClosed as e:
                check("participant before window → SessionClosed", e.reason, "not_started")
            url, role, _ = await domain.join(db, profiles["jadmin"], future)
            check("moderator joins early", role, "moderator")

            await domain.update_session(db, live, ConferenceSessionUpdate(status="cancelled"))
            try:
                await domain.join(db, profiles["owner"], live)
                check("cancelled blocks moderator too", None, "cancelled")
            except SessionClosed as e:
                check("cancelled blocks moderator too", e.reason, "cancelled")

            print("список:")
            rows = await domain.list_sessions(db, event.id)
            check("two sessions, ordered by start", [r.title for r in rows], ["Секция идёт", "Пленарное"])

            print("выключено:")
            saved = settings.MEET_JWT_SECRET
            settings.MEET_JWT_SECRET = None
            try:
                make_meet_token(profile_id=uuid.uuid4(), name="x", room="r", role="participant", session_id=1)
                check("no secret → MeetDisabled", False, True)
            except MeetDisabled:
                check("no secret → MeetDisabled", True, True)
            finally:
                settings.MEET_JWT_SECRET = saved
        finally:
            await db.rollback()
            await db.execute(delete(ConferenceSession).where(ConferenceSession.issue_id.in_(ids["events"])))
            await db.execute(delete(ArticleAuthor).where(ArticleAuthor.article_id == ids["talk"]))
            await db.execute(delete(Article).where(Article.id == ids["talk"]))
            await db.execute(delete(ConferenceSection).where(ConferenceSection.id.in_(ids["sections"])))
            await db.execute(delete(JournalAdmin).where(JournalAdmin.journal_id.in_(ids["journals"])))
            await db.execute(delete(Issue).where(Issue.id.in_(ids["events"])))
            await db.execute(delete(Journal).where(Journal.id.in_(ids["journals"])))
            await db.execute(delete(Profile).where(Profile.id.in_(ids["users"])))
            await db.execute(delete(User).where(User.id.in_(ids["users"])))
            await db.commit()

    print(f"\n{sum(results)}/{len(results)} passed")
    raise SystemExit(0 if all(results) else 1)


asyncio.run(main())

"""Фаза 4 — проверка матрицы авторизации против реальной БД.

Создаёт временные фикстуры (owner / journal-admin / publisher-admin / plain +
journal J, issue I, publisher P, 3 статьи), гоняет предикаты authz и инварианты
AdminDomain, затем удаляет всё за собой (FK-безопасный порядок).
"""
import asyncio
import uuid

from sqlalchemy import delete, select

from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article, Issue, Journal, JournalAdmin, Profile, Publisher, User,
)
from src.domain.authz import can_write_journal, can_write_issue, can_write_article
from src.domain.admin import AdminDomain, AdminError, NotOwner

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results = []


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  [{PASS if ok else FAIL}] {name}: got={got} want={want}")


async def main():
    tag = uuid.uuid4().hex[:8]
    admin = AdminDomain()
    async with AsyncSessionLocal() as db:
        # ---- users + profiles (roles) ----
        def mk_user(role, name):
            u = User(id=uuid.uuid4(), email=f"authz-{tag}-{name}@example.invalid")
            db.add(u)
            return u
        owner_u = mk_user("owner", "owner")
        jadmin_u = mk_user("admin", "jadmin")
        jadmin2_u = mk_user("admin", "jadmin2")
        padmin_u = mk_user("admin", "padmin")
        plain_u = mk_user("authenticated", "plain")
        blocked_u = mk_user("user", "blocked")
        await db.flush()
        for u, role in [(owner_u, "owner"), (jadmin_u, "admin"),
                        (jadmin2_u, "admin"), (padmin_u, "admin"),
                        (plain_u, "authenticated"), (blocked_u, "user")]:
            db.add(Profile(id=u.id, role=role, full_name=f"AuthZ {role}"))
        await db.flush()

        # ---- journals J, J2; issue I under J; publisher P (padmin) ----
        jrn = Journal(name=f"AuthZ J {tag}", slug=f"authz-j-{tag}")
        jrn2 = Journal(name=f"AuthZ J2 {tag}", slug=f"authz-j2-{tag}")
        db.add_all([jrn, jrn2])
        await db.flush()
        iss = Issue(journal_id=jrn.id, title=f"AuthZ issue {tag}")
        db.add(iss)
        pub = Publisher(slug=f"authz-p-{tag}", name=f"AuthZ P {tag}", admin_id=padmin_u.id)
        db.add(pub)
        await db.flush()

        # jadmin attached to J only
        db.add(JournalAdmin(journal_id=jrn.id, user_id=jadmin_u.id))
        # jadmin2 attached to J2 only
        db.add(JournalAdmin(journal_id=jrn2.id, user_id=jadmin2_u.id))
        await db.flush()

        # ---- articles ----
        a_journal = Article(title=f"AuthZ art-journal {tag}", issue_id=iss.id,
                            slug=f"authz-aj-{tag}")
        a_standalone = Article(title=f"AuthZ art-standalone {tag}", admin_id=plain_u.id,
                               slug=f"authz-as-{tag}")  # admin_id = plain_u
        a_pub = Article(title=f"AuthZ art-pub {tag}", publisher_id=pub.id,
                        slug=f"authz-ap-{tag}")
        db.add_all([a_journal, a_standalone, a_pub])
        await db.flush()

        # ================= journals (owner-only) =================
        print("journals (owner-only write):")
        check("owner", await can_write_journal("owner"), True)
        check("jadmin", await can_write_journal("admin"), False)
        check("authenticated", await can_write_journal("authenticated"), False)

        # ================= issues (owner or journal_admin of J) =================
        print("issues under J:")
        check("owner", await can_write_issue(db, role="owner", user_id=owner_u.id, journal_id=jrn.id), True)
        check("jadmin(J)", await can_write_issue(db, role="admin", user_id=jadmin_u.id, journal_id=jrn.id), True)
        check("jadmin2(J2) on J", await can_write_issue(db, role="admin", user_id=jadmin2_u.id, journal_id=jrn.id), False)
        check("padmin on J", await can_write_issue(db, role="admin", user_id=padmin_u.id, journal_id=jrn.id), False)
        check("plain on J", await can_write_issue(db, role="authenticated", user_id=plain_u.id, journal_id=jrn.id), False)

        # ================= articles =================
        async def cwa(role, uid, art):
            return await can_write_article(
                db, role=role, user_id=uid,
                issue_id=art.issue_id, admin_id=art.admin_id, publisher_id=art.publisher_id)

        print("article a_journal (issue under J):")
        check("owner", await cwa("owner", owner_u.id, a_journal), True)
        check("jadmin(J)", await cwa("admin", jadmin_u.id, a_journal), True)
        check("jadmin2(J2)", await cwa("admin", jadmin2_u.id, a_journal), False)
        check("padmin", await cwa("admin", padmin_u.id, a_journal), False)
        check("plain", await cwa("authenticated", plain_u.id, a_journal), False)

        print("article a_standalone (admin_id = plain_u):")
        check("owner", await cwa("owner", owner_u.id, a_standalone), True)
        check("plain (is admin_id)", await cwa("authenticated", plain_u.id, a_standalone), True)
        check("jadmin (not admin_id)", await cwa("admin", jadmin_u.id, a_standalone), False)
        check("padmin (not admin_id)", await cwa("admin", padmin_u.id, a_standalone), False)

        print("article a_pub (publisher_id = P, admin=padmin):")
        check("owner", await cwa("owner", owner_u.id, a_pub), True)
        check("padmin(P)", await cwa("admin", padmin_u.id, a_pub), True)
        check("jadmin (other)", await cwa("admin", jadmin_u.id, a_pub), False)
        check("plain", await cwa("authenticated", plain_u.id, a_pub), False)

        # ================= AdminDomain invariants =================
        print("AdminDomain guards:")
        try:
            await admin.get_all_profiles(db, caller_is_owner=False)
            check("get_all_profiles non-owner raises", False, True)
        except NotOwner:
            check("get_all_profiles non-owner raises", True, True)

        try:
            await admin.set_user_role(db, caller_is_owner=False, target_user=plain_u.id, new_role="admin")
            check("set_user_role non-owner raises", False, True)
        except NotOwner:
            check("set_user_role non-owner raises", True, True)

        try:
            await admin.set_user_role(db, caller_is_owner=True, target_user=plain_u.id, new_role="superuser")
            check("set_user_role bad role raises", False, True)
        except AdminError:
            check("set_user_role bad role raises", True, True)

        try:
            await admin.set_user_role(db, caller_is_owner=True, target_user=owner_u.id, new_role="authenticated")
            check("set_user_role on owner raises", False, True)
        except AdminError:
            check("set_user_role on owner raises", True, True)

        # legit: promote plain -> admin
        await admin.set_user_role(db, caller_is_owner=True, target_user=plain_u.id, new_role="admin")
        role_now = (await db.execute(select(Profile.role).where(Profile.id == plain_u.id))).scalar_one()
        check("set_user_role plain->admin applied", role_now, "admin")

        # set_journal_admin attach promotes blocked_u? blocked role 'user' -> attach makes admin
        await admin.set_journal_admin(db, caller_is_owner=True, target_journal=jrn.id, target_user=blocked_u.id, attach=True)
        ja_exists = (await db.execute(select(JournalAdmin).where(
            JournalAdmin.journal_id == jrn.id, JournalAdmin.user_id == blocked_u.id))).scalars().first()
        role_b = (await db.execute(select(Profile.role).where(Profile.id == blocked_u.id))).scalar_one()
        check("set_journal_admin attach inserts row", ja_exists is not None, True)
        check("set_journal_admin attach promotes to admin", role_b, "admin")

        # detach
        await admin.set_journal_admin(db, caller_is_owner=True, target_journal=jrn.id, target_user=blocked_u.id, attach=False)
        ja_gone = (await db.execute(select(JournalAdmin).where(
            JournalAdmin.journal_id == jrn.id, JournalAdmin.user_id == blocked_u.id))).scalars().first()
        check("set_journal_admin detach removes row", ja_gone is None, True)

        # ---- cleanup (FK-safe order) ----
        ids = [owner_u.id, jadmin_u.id, jadmin2_u.id, padmin_u.id, plain_u.id, blocked_u.id]
        await db.execute(delete(Article).where(Article.id.in_([a_journal.id, a_standalone.id, a_pub.id])))
        await db.execute(delete(JournalAdmin).where(JournalAdmin.journal_id.in_([jrn.id, jrn2.id])))
        await db.execute(delete(Issue).where(Issue.id == iss.id))
        await db.execute(delete(Publisher).where(Publisher.id == pub.id))
        await db.execute(delete(Journal).where(Journal.id.in_([jrn.id, jrn2.id])))
        await db.execute(delete(Profile).where(Profile.id.in_(ids)))
        await db.execute(delete(User).where(User.id.in_(ids)))
        await db.commit()

    total = len(results)
    passed = sum(results)
    print(f"\n=== {passed}/{total} checks passed ===")
    if passed != total:
        raise SystemExit(1)


asyncio.run(main())

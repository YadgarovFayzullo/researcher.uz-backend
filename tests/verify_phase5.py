"""Фаза 5 — проверка сервисов (RPC → код) против реальной БД.

Создаёт временные фикстуры (owner/researcher/plain + журнал J, выпуск I, 3 статьи,
взаимодействия, external_citations, article_references), прогоняет stats/citations/
researcher/admin-сервисы и сверяет семантику с исходными RPC, затем удаляет всё
за собой (FK-безопасный порядок).

Запуск: PYTHONPATH=. python tests/verify_phase5.py
"""
import asyncio
import uuid

from sqlalchemy import delete, select

from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    ArticleInteraction,
    ArticleReference,
    ExternalCitation,
    Issue,
    Journal,
    Profile,
    ResearcherWork,
    User,
)
from src.domain.admin import AdminDomain
from src.domain.citations import CitationsDomain, NotOwner
from src.domain.researcher import CabinetError, ResearcherDomain
from src.domain.stats import StatsDomain

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results = []


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  [{PASS if ok else FAIL}] {name}: got={got!r} want={want!r}")


def check_true(name, got):
    results.append(bool(got))
    print(f"  [{PASS if got else FAIL}] {name}: {got!r}")


async def main():
    tag = uuid.uuid4().hex[:8]
    orcid = "0000-0002-1825-0097"  # валидный ORCID (контрольная сумма ок)
    stats = StatsDomain
    cit = CitationsDomain()
    cab = ResearcherDomain()
    admin = AdminDomain()

    async with AsyncSessionLocal() as db:
        # ---------- users + profiles ----------
        owner_u = User(id=uuid.uuid4(), email=f"p5-{tag}-owner@example.invalid")
        rsch_u = User(id=uuid.uuid4(), email=f"p5-{tag}-rsch@example.invalid")
        plain_u = User(id=uuid.uuid4(), email=f"p5-{tag}-plain@example.invalid")
        db.add_all([owner_u, rsch_u, plain_u])
        await db.flush()
        db.add_all([
            Profile(id=owner_u.id, role="owner", full_name="P5 Owner"),
            Profile(id=rsch_u.id, role="authenticated", full_name="P5 Researcher",
                    orcid_id=orcid, workplace="Uni", country="UZ", bio="bio", education="PhD"),
            Profile(id=plain_u.id, role="admin", full_name="P5 Admin"),
        ])
        await db.flush()

        # ---------- journal J, issue I ----------
        jrn = Journal(name=f"P5 J {tag}", slug=f"p5-j-{tag}")
        db.add(jrn)
        await db.flush()
        iss = Issue(journal_id=jrn.id, title=f"P5 issue {tag}")
        db.add(iss)
        await db.flush()

        # ---------- 3 articles ----------
        a1 = Article(title=f"P5 A1 {tag}", slug=f"p5-a1-{tag}", issue_id=iss.id,
                     doi="10.1234/TEST-A", publication_year=2024)
        a2 = Article(title=f"P5 A2 {tag}", slug=f"p5-a2-{tag}", issue_id=iss.id,
                     doi="https://doi.org/10.1234/TEST-B", publication_year=2023)
        a3 = Article(title=f"P5 A3 {tag}", slug=f"p5-a3-{tag}", issue_id=iss.id)
        db.add_all([a1, a2, a3])
        await db.flush()

        # ---------- interactions ----------
        # a1: 3 views (разные IP), 1 download; a2: 1 view.
        # Пишем через StatsDomain, а не вставкой в article_interactions: сервисы
        # ниже читают денормализованные счётчики articles.views_count /
        # downloads_count, а двигает их только этот путь (StatsDomain._bump).
        await db.commit()
        for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
            await stats.record_view(db, a1.id, ip)
        await stats.record_download(db, a1.id, "10.0.0.4")
        await stats.record_view(db, a2.id, "10.0.0.5")
        # ---------- citations graph ----------
        # a2 и a3 цитируют a1 (внутренний счётчик a1 = 2); external a1 = 7
        db.add_all([
            ArticleReference(article_id=a2.id, cited_article_id=a1.id, position=1),
            ArticleReference(article_id=a3.id, cited_article_id=a1.id, position=1),
            ExternalCitation(article_id=a1.id, doi="10.1234/test-a", cited_by_count=7),
        ])
        await db.flush()

        # ======================= STATS =======================
        print("stats.get_article_stats_batch:")
        batch = {r["article_id"]: r for r in await stats.get_article_stats_batch(db, [a1.id, a2.id, a3.id])}
        check("a1 views", batch[a1.id]["views"], 3)
        check("a1 downloads", batch[a1.id]["downloads"], 1)
        check("a2 views", batch[a2.id]["views"], 1)
        # Раньше батч агрегировал сам лог и статью без взаимодействий пропускал.
        # После денормализации он читает счётчики самой статьи, поэтому строка
        # есть у каждой запрошенной — просто с нулями.
        check("a3 присутствует с нулями", batch[a3.id]["views"], 0)

        print("stats.get_journal_stats:")
        jstats = {r["journal_id"]: r for r in await stats.get_journal_stats(db)}
        check("J views", jstats.get(jrn.id, {}).get("views"), 4)   # 3 (a1) + 1 (a2)
        check("J downloads", jstats.get(jrn.id, {}).get("downloads"), 1)

        print("stats.get_platform_stats:")
        plat = await stats.get_platform_stats(db)
        check_true("totalViews is int >0", isinstance(plat["totalViews"], int) and plat["totalViews"] >= 4)
        check_true("totalDownloads int >0", isinstance(plat["totalDownloads"], int) and plat["totalDownloads"] >= 1)

        print("stats.get_journal_analytics:")
        ana = (await stats.get_journal_analytics(db, [jrn.id]))[0]
        check("journal_name", ana["journal_name"], f"P5 J {tag}")
        check("total_views", ana["total_views"], 4)
        check("total_downloads", ana["total_downloads"], 1)
        check("total_articles", ana["total_articles"], 3)

        print("stats.get_top_articles (window 3650d):")
        top = await stats.get_top_articles(db, [jrn.id], 3650)
        check("top[0] is a1", top[0]["article_slug"], f"p5-a1-{tag}")
        check("top[0] views", top[0]["views_count"], 3)

        print("stats.get_daily_stats (window 3650d):")
        daily = await stats.get_daily_stats(db, [jrn.id], 3650)
        check("daily total views", sum(d["views_count"] for d in daily), 4)

        print("stats.add_interaction (view dedup by IP):")
        v1 = await stats.add_interaction(db, article_id=a3.id, ip_address="10.9.9.9", interaction_type="view")
        v2 = await stats.add_interaction(db, article_id=a3.id, ip_address="10.9.9.9", interaction_type="view")
        check("first view accepted", v1, True)
        check("second view (same IP, <1h) rejected", v2, False)
        like_ok = await stats.add_interaction(db, article_id=a3.id, ip_address="10.9.9.9", interaction_type="like")
        dislike_ok = await stats.add_interaction(db, article_id=a3.id, ip_address="10.9.9.9", interaction_type="dislike")
        check("like accepted", like_ok, True)
        check("dislike replaces like", dislike_ok, True)
        a3_stats = await stats.get_article_stats(db, a3.id)
        check("a3 net like=0 dislike=1", (a3_stats["likes"], a3_stats["dislikes"]), (0, 1))
        bad = await stats.add_interaction(db, article_id=a3.id, ip_address="x", interaction_type="bogus")
        check("bogus type rejected", bad, False)

        print("stats.increment_article_views:")
        before = (await stats.get_article_stats(db, a2.id))["views"]
        await stats.increment_article_views(db, a2.id)
        after = (await stats.get_article_stats(db, a2.id))["views"]
        check("view +1", after - before, 1)

        # ======================= CITATIONS =======================
        print("citations.get_article_citations:")
        cc = {r["article_id"]: r for r in await cit.get_article_citations(db, [a1.id, a2.id])}
        check("a1 internal=2", cc[a1.id]["internal_count"], 2)
        check("a1 external=7", cc[a1.id]["external_count"], 7)
        check("a1 cited_by=max(2,7)=7", cc[a1.id]["cited_by"], 7)
        check("a2 internal=0", cc[a2.id]["internal_count"], 0)
        check("order preserved / a2 present", cc[a2.id]["cited_by"], 0)

        print("citations.get_citing_articles(a1):")
        citing = {r["slug"] for r in await cit.get_citing_articles(db, a1.id)}
        check("a2 & a3 cite a1", citing, {f"p5-a2-{tag}", f"p5-a3-{tag}"})

        print("citations.match_articles_by_doi:")
        m = await cit.match_articles_by_doi(db, ["10.1234/test-a", "https://doi.org/10.1234/TEST-B"])
        matched = {r["norm_doi"]: r["id"] for r in m}
        check("test-a → a1", matched.get("10.1234/test-a"), a1.id)
        check("test-b (normalized) → a2", matched.get("10.1234/test-b"), a2.id)

        print("citations.upsert_external_citations:")
        try:
            await cit.upsert_external_citations(db, caller_is_owner=False, rows=[{"article_id": a2.id, "cited_by_count": 5}])
            check("non-owner raises", False, True)
        except NotOwner:
            check("non-owner raises NotOwner", True, True)
        n = await cit.upsert_external_citations(db, caller_is_owner=True,
              rows=[{"article_id": a2.id, "doi": "10.1234/test-b", "cited_by_count": 5, "counts_by_year": [{"year": 2024, "count": 5}]}])
        check("upserted 1 row", n, 1)
        ec = (await db.execute(select(ExternalCitation).where(ExternalCitation.article_id == a2.id))).scalars().first()
        check("a2 external cached=5", ec.cited_by_count, 5)

        # ======================= RESEARCHER CABINET =======================
        print("researcher.update_my_profile:")
        await cab.update_my_profile(db, user_id=rsch_u.id, workplace="", country="KR")
        prof = (await db.execute(select(Profile).where(Profile.id == rsch_u.id))).scalars().first()
        check("workplace cleared ('' → NULL)", prof.workplace, None)
        check("country updated", prof.country, "KR")
        check("bio untouched (None arg)", prof.bio, "bio")

        print("researcher.claim_article / idempotent / unclaim:")
        await cab.claim_article(db, user_id=rsch_u.id, article_id=a1.id)
        cnt1 = (await db.execute(select(ArticleAuthor).where(ArticleAuthor.article_id == a1.id, ArticleAuthor.profile_id == rsch_u.id))).scalars().all()
        check("claim adds 1 author row", len(cnt1), 1)
        await cab.claim_article(db, user_id=rsch_u.id, article_id=a1.id)
        cnt2 = (await db.execute(select(ArticleAuthor).where(ArticleAuthor.article_id == a1.id, ArticleAuthor.profile_id == rsch_u.id))).scalars().all()
        check("second claim is no-op (idempotent)", len(cnt2), 1)
        await cab.unclaim_article(db, user_id=rsch_u.id, article_id=a1.id)
        cnt3 = (await db.execute(select(ArticleAuthor).where(ArticleAuthor.article_id == a1.id, ArticleAuthor.profile_id == rsch_u.id))).scalars().all()
        check("unclaim removes row", len(cnt3), 0)

        print("researcher.claim_articles_by_dois:")
        claimed = await cab.claim_articles_by_dois(db, user_id=rsch_u.id, dois=["10.1234/test-a", "https://doi.org/10.1234/test-b"])
        check("claimed a1 & a2 by DOI", claimed, 2)
        claimed_again = await cab.claim_articles_by_dois(db, user_id=rsch_u.id, dois=["10.1234/test-a"])
        check("re-claim no-op", claimed_again, 0)

        print("researcher.claim_article without ORCID → error:")
        try:
            await cab.claim_article(db, user_id=plain_u.id, article_id=a1.id)
            check("no-ORCID raises", False, True)
        except CabinetError:
            check("no-ORCID raises CabinetError", True, True)

        print("researcher.import_orcid_works (full replace):")
        n1 = await cab.import_orcid_works(db, user_id=rsch_u.id, works=[
            {"put_code": "111", "title": "W1", "year": "2020", "doi": "10.1/x"},
            {"put_code": "222", "title": "W2", "year": 2021},
            {"put_code": "111", "title": "dup"},  # on conflict do nothing
        ])
        check("import stores 2 (dup put_code ignored)", n1, 2)
        n2 = await cab.import_orcid_works(db, user_id=rsch_u.id, works=[{"put_code": "333", "title": "W3"}])
        check("re-import replaces (now 1)", n2, 1)

        print("researcher.get_researcher_profile:")
        card = await cab.get_researcher_profile(db, orcid)
        check("card orcid", card["orcid"], orcid)
        check("card country=KR", card["country"], "KR")
        check_true("no id/role/email leaked", set(card.keys()) == {"full_name", "orcid", "avatar_url", "workplace", "country", "bio", "education"})
        works = await cab.list_researcher_works(db, orcid)
        check("list_researcher_works=1", len(works), 1)

        # ======================= ADMIN =======================
        print("admin.get_editor_options:")
        opts = await admin.get_editor_options(db, caller_is_owner=True)
        emails = {o["email"] for o in opts}
        check_true("owner in options", owner_u.email in emails)
        check_true("admin in options", plain_u.email in emails)
        check_true("researcher(authenticated) NOT in options", rsch_u.email not in emails)

        # ---------------- cleanup (FK-safe) ----------------
        art_ids = [a1.id, a2.id, a3.id]
        await db.execute(delete(ArticleAuthor).where(ArticleAuthor.article_id.in_(art_ids)))
        await db.execute(delete(ArticleReference).where(ArticleReference.article_id.in_(art_ids)))
        await db.execute(delete(ExternalCitation).where(ExternalCitation.article_id.in_(art_ids)))
        await db.execute(delete(ArticleInteraction).where(ArticleInteraction.article_id.in_(art_ids)))
        await db.execute(delete(ResearcherWork).where(ResearcherWork.orcid == orcid))
        await db.execute(delete(Article).where(Article.id.in_(art_ids)))
        await db.execute(delete(Issue).where(Issue.id == iss.id))
        await db.execute(delete(Journal).where(Journal.id == jrn.id))
        ids = [owner_u.id, rsch_u.id, plain_u.id]
        await db.execute(delete(Profile).where(Profile.id.in_(ids)))
        await db.execute(delete(User).where(User.id.in_(ids)))
        await db.commit()

        # verify no leftovers
        left = (await db.execute(select(Article).where(Article.id.in_(art_ids)))).scalars().all()
        check("0 leftover fixtures", len(left), 0)

    total = len(results)
    passed = sum(results)
    print(f"\n=== {passed}/{total} checks passed ===")
    if passed != total:
        raise SystemExit(1)


asyncio.run(main())

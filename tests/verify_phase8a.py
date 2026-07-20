"""Фаза 8a — проверка новых ресурсов (issues, publishers, conference_sections,
article_authors, article_references, saved_articles) против реальной БД.

Создаёт временные фикстуры (owner/admin/plain + издатель, журнал, 2 выпуска,
секции, статьи, взаимодействия), прогоняет доменные сервисы, проверяет
семантику (порядок, счётчики, идемпотентность, откреп вместо каскадного
удаления) и удаляет всё за собой в FK-безопасном порядке.

Запуск: PYTHONPATH=. python tests/verify_phase8a.py
"""
import asyncio
import uuid

from sqlalchemy import delete, select

from src.domain.content import (
    AuthorDomain,
    LibraryDomain,
    ReferenceDomain,
    SectionDomain,
)
from src.domain.issue import IssueDomain
from src.domain.publisher import PublisherDomain
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    ArticleInteraction,
    ArticleReference,
    ConferenceSection,
    Issue,
    Journal,
    Profile,
    Publisher,
    SavedArticle,
    User,
)
from src.schemas.content import AuthorIn, ReferenceIn, SectionCreate, SectionUpdate
from src.schemas.issue import IssueCreate, IssueUpdate
from src.schemas.publisher import PublisherCreate, PublisherUpdate

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results: list[bool] = []


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  [{PASS if ok else FAIL}] {name}: got={got!r} want={want!r}")


def check_true(name, got):
    results.append(bool(got))
    print(f"  [{PASS if got else FAIL}] {name}: {got!r}")


async def _sweep_leftovers(db):
    """Снести фикстуры прошлых прогонов. Все они помечены префиксом 'P8a '
    в человекочитаемом поле, слугами 'p8a-' и почтой на .invalid."""
    art_ids = (
        await db.execute(select(Article.id).where(Article.title.like("P8a %")))
    ).scalars().all()
    iss_ids = (
        await db.execute(select(Issue.id).where(Issue.title.like("P8a %")))
    ).scalars().all()
    prof_ids = (
        await db.execute(select(Profile.id).where(Profile.full_name.like("P8a %")))
    ).scalars().all()

    for stmt in (
        delete(SavedArticle).where(SavedArticle.article_id.in_(art_ids)),
        delete(ArticleReference).where(ArticleReference.article_id.in_(art_ids)),
        delete(ArticleAuthor).where(ArticleAuthor.article_id.in_(art_ids)),
        delete(ArticleInteraction).where(ArticleInteraction.article_id.in_(art_ids)),
        delete(Article).where(Article.id.in_(art_ids)),
        delete(ConferenceSection).where(ConferenceSection.issue_id.in_(iss_ids)),
        delete(Issue).where(Issue.id.in_(iss_ids)),
        delete(Journal).where(Journal.name.like("P8a %")),
        delete(Publisher).where(Publisher.name.like("P8a %")),
        delete(Profile).where(Profile.id.in_(prof_ids)),
        delete(User).where(User.id.in_(prof_ids)),
    ):
        await db.execute(stmt)
    await db.commit()


async def main():
    tag = uuid.uuid4().hex[:8]
    issues = IssueDomain()
    pubs = PublisherDomain()
    sections = SectionDomain()
    authors = AuthorDomain()
    refs = ReferenceDomain()
    lib = LibraryDomain()
    orcid = "0000-0002-1825-0097"

    created = {}

    async with AsyncSessionLocal() as db:
        # Сначала — подмести за упавшими прогонами. Уборка ниже стоит в конце
        # тела, а не в finally, поэтому исключение посреди проверок оставляет
        # фикстуры в БД (так уже случилось: три статьи 'P8a ...' пережили
        # падение на KeyError). Дешевле снести их по префиксу на входе, чем
        # ловить расхождение счётчиков через неделю.
        await _sweep_leftovers(db)

        # ------------------------------------------------ фикстуры
        owner_u = User(id=uuid.uuid4(), email=f"p8a-{tag}-owner@example.invalid")
        admin_u = User(id=uuid.uuid4(), email=f"p8a-{tag}-admin@example.invalid")
        db.add_all([owner_u, admin_u])
        await db.flush()
        db.add_all([
            Profile(id=owner_u.id, role="owner", full_name="P8a Owner"),
            Profile(id=admin_u.id, role="admin", full_name="P8a Admin"),
        ])
        await db.flush()

        publisher = Publisher(
            slug=f"p8a-pub-{tag}", name=f"P8a Publisher {tag}", admin_id=admin_u.id
        )
        journal = Journal(name=f"P8a J {tag}", slug=f"p8a-j-{tag}")
        db.add_all([publisher, journal])
        await db.flush()

        iss_a = Issue(journal_id=journal.id, title=f"P8a issue A {tag}", year=2024)
        iss_b = Issue(journal_id=journal.id, title=f"P8a issue B {tag}", year=2023)
        db.add_all([iss_a, iss_b])
        await db.flush()

        # iss_a: 2 статьи, iss_b: 0 (проверяем, что пустой выпуск не пропадает)
        a1 = Article(title=f"P8a A1 {tag}", slug=f"p8a-a1-{tag}", issue_id=iss_a.id)
        a2 = Article(title=f"P8a A2 {tag}", slug=f"p8a-a2-{tag}", issue_id=iss_a.id)
        a3 = Article(
            title=f"P8a A3 {tag}", slug=f"p8a-a3-{tag}", publisher_id=publisher.id
        )
        db.add_all([a1, a2, a3])
        await db.flush()
        db.add_all([
            ArticleInteraction(article_id=a1.id, view=1),
            ArticleInteraction(article_id=a1.id, view=1),
            ArticleInteraction(article_id=a1.id, download=1),
        ])
        await db.commit()
        created = dict(
            users=[owner_u.id, admin_u.id],
            journal=journal.id,
            publisher=publisher.id,
            issues=[iss_a.id, iss_b.id],
            articles=[a1.id, a2.id, a3.id],
        )

        # ------------------------------------------------ issues
        print("\n--- issues ---")
        listed = await issues.list_issues(db, journal_id=journal.id)
        by_id = {r["id"]: r for r in listed}
        check("выпусков журнала", len(listed), 2)
        check("article_count непустого", by_id[iss_a.id]["article_count"], 2)
        check("пустой выпуск отдаётся с 0", by_id[iss_b.id]["article_count"], 0)
        # Домен отдаёт имена колонок (`meta`); в `metadata` его переименовывает
        # схема на сериализации — см. _META_IN в src/schemas/issue.py.
        check("meta отдаётся как есть", by_id[iss_a.id]["meta"], {})
        check(
            "фильтр по году",
            [r["id"] for r in await issues.list_issues(db, journal_id=journal.id, year=2023)],
            [iss_b.id],
        )
        check("годы (desc, distinct)", await issues.list_years(db, journal.id), [2024, 2023])

        new_iss = await issues.create_issue(
            db, IssueCreate(journal_id=journal.id, volume="7", issue="2", year=2025)
        )
        created["issues"].append(new_iss.id)
        check("создан выпуск", (new_iss.volume, new_iss.issue, new_iss.year), ("7", "2", 2025))
        upd = await issues.update_issue(db, new_iss.id, IssueUpdate(volume="8"))
        check("update меняет volume", upd.volume, "8")
        check("update не затирает непереданное (issue)", upd.issue, "2")
        check("update не затирает непереданное (year)", upd.year, 2025)

        # ------------------------------------------------ publishers
        print("\n--- publishers ---")
        check_true("get by slug", (await pubs.get_by_slug(db, publisher.slug)) is not None)
        check_true("get by id", (await pubs.get_by_id(db, publisher.id)) is not None)
        check("несуществующий slug → None", await pubs.get_by_slug(db, f"nope-{tag}"), None)
        mine = await pubs.list_publishers(db, admin_id=admin_u.id)
        check("издатели админа", [p.id for p in mine], [publisher.id])
        check("публикаций у издателя", await pubs.count_publications(db, publisher.id), 1)

        # admin_id меняет только owner
        await pubs.update_publisher(
            db, publisher.id, PublisherUpdate(admin_id=owner_u.id), allow_admin_change=False
        )
        p = await pubs.get_by_id(db, publisher.id)
        check("не-owner не переназначает admin_id", p.admin_id, admin_u.id)
        await pubs.update_publisher(
            db, publisher.id, PublisherUpdate(admin_id=owner_u.id), allow_admin_change=True
        )
        p = await pubs.get_by_id(db, publisher.id)
        check("owner переназначает admin_id", p.admin_id, owner_u.id)

        # ------------------------------------------------ conference_sections
        print("\n--- conference_sections ---")
        s1 = await sections.create_section(
            db, SectionCreate(issue_id=iss_a.id, title="S1", position=0)
        )
        s2 = await sections.create_section(
            db, SectionCreate(issue_id=iss_a.id, title="S2", position=1)
        )
        s3 = await sections.create_section(
            db, SectionCreate(issue_id=iss_a.id, title="S3", position=2)
        )
        check("секции по порядку", [s.title for s in await sections.list_sections(db, iss_a.id)],
              ["S1", "S2", "S3"])
        await sections.reorder(db, iss_a.id, [s3.id, s1.id, s2.id])
        check("reorder применён", [s.title for s in await sections.list_sections(db, iss_a.id)],
              ["S3", "S1", "S2"])
        # неизвестные id игнорируются, неупомянутые уезжают в конец
        await sections.reorder(db, iss_a.id, [s2.id, 999999])
        check("reorder: частичный список + мусорный id",
              [s.title for s in await sections.list_sections(db, iss_a.id)], ["S2", "S3", "S1"])
        await sections.update_section(db, s1.id, SectionUpdate(title="S1-upd"))
        check("update секции", (await sections.get_section(db, s1.id)).title, "S1-upd")

        # доклад в секции — при удалении секции должен остаться, но открепиться
        a1.section_id = s1.id
        await db.commit()
        await sections.delete_section(db, s1.id)
        await db.refresh(a1)
        check("секция удалена", await sections.get_section(db, s1.id), None)
        check_true("статья пережила удаление секции", (await db.get(Article, a1.id)) is not None)
        check("статья откреплена от секции", a1.section_id, None)

        # ------------------------------------------------ article_authors
        print("\n--- article_authors ---")
        await authors.replace_for_article(db, a1.id, [
            AuthorIn(author_name="Zeta", author_order=1, orcid=orcid),
            AuthorIn(author_name="Alpha", author_order=0),
        ])
        got = await authors.list_by_article(db, a1.id)
        check("авторы по author_order", [a.author_name for a in got], ["Alpha", "Zeta"])
        # полная замена, а не добавление
        await authors.replace_for_article(db, a1.id, [AuthorIn(author_name="Solo", author_order=0)])
        got = await authors.list_by_article(db, a1.id)
        check("replace заменяет, а не дополняет", [a.author_name for a in got], ["Solo"])
        await authors.replace_for_article(db, a1.id, [
            AuthorIn(author_name="Zeta", author_order=0, orcid=orcid),
        ])
        pubs_by_orcid = await authors.list_publications_by_orcid(db, orcid)
        check("публикаций по ORCID", len(pubs_by_orcid), 1)
        check("в выдаче есть журнал", pubs_by_orcid[0]["article"]["journal_name"], journal.name)
        check("пустой ORCID → пусто", await authors.list_publications_by_orcid(db, "0000-0000-0000-0000"), [])

        # ------------------------------------------------ article_references
        print("\n--- article_references ---")
        await refs.replace_for_article(db, a2.id, [
            ReferenceIn(raw="ref-2", position=1),
            ReferenceIn(raw="ref-1", position=0, cited_article_id=a1.id),
        ])
        got = await refs.list_by_article(db, a2.id)
        check("ссылки по position", [r["raw"] for r in got], ["ref-1", "ref-2"])
        check("cited_title подтянут", got[0]["cited_title"], a1.title)
        check("без cited — None", got[1]["cited_title"], None)
        cmap = await refs.citing_map(db, [a1.id, a3.id])
        check("citing_map: кто цитирует a1", cmap[a1.id], [a2.id])
        check("citing_map: никто не цитирует a3", cmap[a3.id], [])
        check("citing_map пустой вход", await refs.citing_map(db, []), {})

        # ------------------------------------------------ saved_articles
        print("\n--- library ---")
        check("первое сохранение", await lib.save(db, owner_u.id, a1.id), True)
        check("повторное сохранение идемпотентно", await lib.save(db, owner_u.id, a1.id), False)
        check("is_saved", await lib.is_saved(db, owner_u.id, a1.id), True)
        saved = await lib.list_saved(db, owner_u.id)
        check("в библиотеке 1 статья", len(saved), 1)
        check("views посчитаны", saved[0]["article"]["views"], 2)
        check("downloads посчитаны", saved[0]["article"]["downloads"], 1)
        check("журнал подтянут", saved[0]["article"]["journal_name"], journal.name)
        check_true("embedding не отдаётся", "embedding" not in saved[0]["article"])
        check("unsave", await lib.unsave(db, owner_u.id, a1.id), True)
        check("повторный unsave", await lib.unsave(db, owner_u.id, a1.id), False)
        check("библиотека пуста", await lib.list_saved(db, owner_u.id), [])

        # ------------------------------------------------ удаление контейнеров
        print("\n--- удаление: откреп, а не каскад ---")
        await issues.delete_issue(db, iss_a.id)
        check("выпуск удалён", await issues.get_issue(db, iss_a.id), None)
        await db.refresh(a1)
        check_true("статья выпуска жива", (await db.get(Article, a1.id)) is not None)
        check("статья откреплена от выпуска", a1.issue_id, None)
        check("секции выпуска удалены",
              (await db.execute(select(ConferenceSection).where(
                  ConferenceSection.issue_id == iss_a.id))).first(), None)

        await pubs.delete_publisher(db, publisher.id)
        await db.refresh(a3)
        check("издатель удалён", await pubs.get_by_id(db, publisher.id), None)
        check_true("публикация издателя жива", (await db.get(Article, a3.id)) is not None)
        check("публикация откреплена от издателя", a3.publisher_id, None)

        # ------------------------------------------------ уборка
        print("\n--- уборка ---")
        aids = created["articles"]
        await db.execute(delete(SavedArticle).where(SavedArticle.article_id.in_(aids)))
        await db.execute(delete(ArticleReference).where(ArticleReference.article_id.in_(aids)))
        await db.execute(delete(ArticleAuthor).where(ArticleAuthor.article_id.in_(aids)))
        await db.execute(delete(ArticleInteraction).where(ArticleInteraction.article_id.in_(aids)))
        await db.execute(delete(Article).where(Article.id.in_(aids)))
        await db.execute(
            delete(ConferenceSection).where(ConferenceSection.issue_id.in_(created["issues"]))
        )
        await db.execute(delete(Issue).where(Issue.id.in_(created["issues"])))
        await db.execute(delete(Journal).where(Journal.id == created["journal"]))
        await db.execute(delete(Publisher).where(Publisher.id == created["publisher"]))
        await db.execute(delete(Profile).where(Profile.id.in_(created["users"])))
        await db.execute(delete(User).where(User.id.in_(created["users"])))
        await db.commit()

        left = 0
        for model, col, vals in [
            (Article, Article.id, aids),
            (Issue, Issue.id, created["issues"]),
            (Journal, Journal.id, [created["journal"]]),
            (Publisher, Publisher.id, [created["publisher"]]),
            (User, User.id, created["users"]),
        ]:
            rows = (await db.execute(select(col).where(col.in_(vals)))).all()
            left += len(rows)
        check("остатков фикстур не осталось", left, 0)

    total, ok = len(results), sum(results)
    print(f"\n{'='*46}\nИтог: {ok}/{total} " + ("— всё зелёное" if ok == total else "— ЕСТЬ ПАДЕНИЯ"))
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

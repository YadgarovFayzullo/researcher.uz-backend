"""Демо-профиль исследователя: копия настоящего профиля для показа без регистрации.

Дизайнеру или клиенту нужно потрогать кабинет исследователя таким, каким его
видит владелец: все кнопки, свои публикации, отчёт в Word. Регистрировать его и
привязывать к настоящим статьям нельзя, поэтому скрипт заводит двойника.

Что создаётся (идемпотентно — повторный запуск досоздаёт недостающее):
* учётка `--email` без рабочего пароля: вход только по ссылке автовхода;
* профиль с ФИО, местом работы, страной, образованием, bio и аватаром исходного
  профиля (`--source-orcid`) и пометкой `profiles.metadata.demo = true`;
* для журналов исходных статей — скрытые демо-журналы с теми же названиями
  (`journals.metadata.demo = true`) и выпуски с теми же годом/томом/номером;
* копии статей: заголовок, аннотация, ключевые слова, страницы, PDF, просмотры
  и скачивания — как у оригинала, но БЕЗ DOI (двух записей с одним настоящим
  DOI быть не должно);
* подписи авторов копий — без ORCID и без карточек (иначе копии всплыли бы на
  настоящих профилях); подпись исходного человека привязана к демо-профилю.

Изоляция описана в src/domain/demo.py: демо-журналы и их статьи не видны в
каталогах, лентах, поиске, sitemap и OAI, не участвуют в проверке дублей и
антиплагиате; демо-профиль не попадает в карусель, страница под noindex,
прикрепить к нему можно только демо-статьи, ORCID и «Это я» закрыты.

Ссылка автовхода — `GET /auth/demo-login?t=<токен>`. В базе только SHA-256
токена и срок, поэтому сам токен печатается один раз: при первом запуске, когда
прежний истёк, или с `--rotate-token` (старая ссылка тут же перестаёт работать).

    ssh root@159.223.153.155 'cd /root/app && docker compose -f docker-compose.prod.yml \\
        exec -T api python scripts/create_demo_researcher.py'
    ... --dry-run            # только план
    ... --rotate-token       # новая ссылка
    ... --days 30            # срок ссылки
"""
from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import or_, select  # noqa: E402

from src.core.security import hash_password  # noqa: E402
from src.domain.demo import DEMO_FLAG, DEMO_LOGIN, article_is_not_demo, demo_login_hash  # noqa: E402
from src.infrastructure.persistence.db import AsyncSessionLocal  # noqa: E402
from src.infrastructure.persistence.models import (  # noqa: E402
    Article,
    ArticleAuthor,
    Identity_,
    Issue,
    Journal,
    Profile,
    User,
)

DEFAULT_SOURCE_ORCID = "0009-0007-4562-4414"
DEFAULT_EMAIL = "demo-researcher@researcher.uz"
DEFAULT_API = "https://api.researcher.uz"

# Поля статьи, которые копируются как есть. Список явный: в таблице есть
# служебные колонки (tsvector, embedding, updated_at) и связи (DOI, admin_id,
# publisher_id, section_id), которые копия унаследовать не должна.
ARTICLE_FIELDS = (
    "title", "title_foreign", "authors", "pages", "annotation",
    "annotation_foreign", "field_of_science", "keywords", "keywords_foreign",
    "pdf", "data", "publication_type", "isbn", "publisher", "publication_year",
    "cover_image", "views_count", "downloads_count", "created_at",
)
PROFILE_FIELDS = ("full_name", "avatar_url", "workplace", "country", "bio", "education", "position")


def _expired(login: dict) -> bool:
    try:
        expires = datetime.fromisoformat(str(login.get("expires_at")))
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= datetime.now(timezone.utc)


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-orcid", default=DEFAULT_SOURCE_ORCID)
    p.add_argument("--email", default=DEFAULT_EMAIL)
    p.add_argument("--days", type=int, default=30, help="срок ссылки автовхода")
    p.add_argument("--rotate-token", action="store_true")
    p.add_argument("--api-base", default=DEFAULT_API)
    p.add_argument("--locale", default="ru")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    async with AsyncSessionLocal() as db:
        source = (
            await db.execute(select(Profile).where(Profile.orcid_id == args.source_orcid))
        ).scalars().first()
        if source is None:
            raise SystemExit(f"Нет профиля с ORCID {args.source_orcid}")

        article_ids = (
            await db.execute(
                select(ArticleAuthor.article_id)
                .where(
                    or_(
                        ArticleAuthor.orcid == args.source_orcid,
                        ArticleAuthor.profile_id == source.id,
                    )
                )
                .distinct()
            )
        ).scalars().all()
        originals = (
            await db.execute(
                select(Article)
                .where(Article.id.in_(article_ids), article_is_not_demo())
                .order_by(Article.data.desc().nullslast(), Article.id)
            )
        ).scalars().all()

        print(f"Источник: {source.full_name} (ORCID {args.source_orcid}), статей: {len(originals)}")
        if args.dry_run:
            for art in originals:
                print(f"  · копия demo-{art.slug or art.id}  {(art.title or '')[:60]}")
            print(f"Учётка: {args.email} (вход только по ссылке)")
            return 0

        # --- учётка и профиль ---------------------------------------------------
        user = (await db.execute(select(User).where(User.email == args.email))).scalars().first()
        if user is None:
            user = User(
                id=uuid.uuid4(),
                email=args.email,
                # Пароль, которого никто не знает: входить по паролю демо не должно.
                password_hash=hash_password(secrets.token_urlsafe(32)),
            )
            db.add(user)
            await db.flush()
            db.add(
                Identity_(
                    user_id=user.id,
                    provider="email",
                    provider_id=str(user.id),
                    identity_data={"email": args.email},
                )
            )
            print(f"+ пользователь {args.email}")
        else:
            print(f"= пользователь {args.email} уже есть")

        profile = (await db.execute(select(Profile).where(Profile.id == user.id))).scalars().first()
        if profile is None:
            profile = Profile(id=user.id, role="authenticated")
            db.add(profile)
        for field in PROFILE_FIELDS:
            setattr(profile, field, getattr(source, field))
        profile.is_public = True

        meta = dict(profile.meta or {})
        meta[DEMO_FLAG] = True
        meta["source_orcid"] = args.source_orcid
        token = None
        login = meta.get(DEMO_LOGIN) or {}
        if args.rotate_token or not login.get("sha256") or _expired(login):
            token = secrets.token_urlsafe(32)
            meta[DEMO_LOGIN] = {
                "sha256": demo_login_hash(token),
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=args.days)).isoformat(),
            }
        # Новый словарь, а не правка на месте: изменение внутри JSONB SQLAlchemy не видит.
        profile.meta = meta
        await db.flush()

        # --- демо-журналы, выпуски, копии статей ---------------------------------
        for art in originals:
            copy_slug = f"demo-{art.slug or art.id}"
            if (await db.execute(select(Article.id).where(Article.slug == copy_slug))).first():
                print(f"= копия {copy_slug} уже есть")
                continue

            src_issue = (
                await db.execute(select(Issue).where(Issue.id == art.issue_id))
            ).scalars().first() if art.issue_id else None
            src_journal = (
                await db.execute(select(Journal).where(Journal.id == src_issue.journal_id))
            ).scalars().first() if src_issue and src_issue.journal_id else None

            issue_id = None
            if src_issue is not None and src_journal is not None:
                j_slug = f"demo-{src_journal.slug or src_journal.id}"
                journal = (
                    await db.execute(select(Journal).where(Journal.slug == j_slug))
                ).scalars().first()
                if journal is None:
                    journal = Journal(
                        name=src_journal.name,
                        slug=j_slug,
                        type=src_journal.type or "journal",
                        publisher=src_journal.publisher,
                        description="Демонстрационная копия журнала для демо-профиля исследователя.",
                        meta={DEMO_FLAG: True, "demo_profile_copy": True},
                    )
                    db.add(journal)
                    await db.flush()
                    print(f"+ демо-журнал «{journal.name}» id={journal.id}")
                elif str((journal.meta or {}).get(DEMO_FLAG)).lower() != "true":
                    raise SystemExit(f"Журнал {j_slug} есть, но не демо — остановка, чтобы не задеть витрину")

                issue = (
                    await db.execute(
                        select(Issue).where(
                            Issue.journal_id == journal.id,
                            Issue.year == src_issue.year,
                            Issue.volume == src_issue.volume,
                            Issue.issue == src_issue.issue,
                        )
                    )
                ).scalars().first()
                if issue is None:
                    issue = Issue(
                        journal_id=journal.id,
                        year=src_issue.year,
                        volume=src_issue.volume,
                        issue=src_issue.issue,
                        title=src_issue.title,
                    )
                    db.add(issue)
                    await db.flush()
                issue_id = issue.id
            else:
                raise SystemExit(
                    f"Статья {art.id} без выпуска и журнала — такую копию не спрятать, остановка"
                )

            copy = Article(
                issue_id=issue_id,
                slug=copy_slug,
                published=True,
                doi=None,
                meta={},
                **{f: getattr(art, f) for f in ARTICLE_FIELDS},
            )
            db.add(copy)
            await db.flush()

            signatures = (
                await db.execute(
                    select(ArticleAuthor)
                    .where(ArticleAuthor.article_id == art.id)
                    .order_by(ArticleAuthor.author_order)
                )
            ).scalars().all()
            mine_real = any(
                (s.orcid == args.source_orcid or s.profile_id == source.id) and not s.from_claim
                for s in signatures
            )
            for s in signatures:
                mine = s.orcid == args.source_orcid or s.profile_id == source.id
                # Строку кабинета копируем, только если настоящей подписи нет.
                if s.from_claim and (not mine or mine_real):
                    continue
                db.add(
                    ArticleAuthor(
                        article_id=copy.id,
                        author_order=s.author_order,
                        author_name=s.author_name,
                        orcid=None,
                        profile_id=user.id if mine else None,
                        is_verified=mine,
                        from_claim=False,
                    )
                )
            print(f"+ копия {copy_slug}  {(art.title or '')[:50]}")

        await db.commit()

        print(f"\nДемо-профиль: https://researcher.uz/{args.locale}/researcher/u/{user.id}")
        if token:
            print("Ссылка автовхода (показывается один раз, действует "
                  f"{args.days} дн.):\n  {args.api_base.rstrip('/')}/auth/demo-login?t={token}&locale={args.locale}")
        else:
            print("Ссылка автовхода уже выдана и действует; новая — с --rotate-token.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

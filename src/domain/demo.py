"""Демо-контент: журнал с `metadata.demo = true` и всё, что лежит под ним.

Демо-журнал заводится, чтобы показать клиенту живую админку прямо на проде:
он получает роль admin и привязку в `journal_admins`, дальше работает как со
своим журналом. Публичная витрина о таком журнале знать не должна — иначе
тестовые выпуски и статьи попадут в каталог, ленты, поиск и в sitemap.

Правило фильтрации:
* журналы — общий список `/journals/` демо не показывает (кроме
  `include_demo=true`, которым пользуется owner-консоль);
* статьи — исключаются из ОБЩИХ лент; адресный запрос (задан issue_id,
  journal_id, section_id, admin_id или publisher_id) отдаёт всё, иначе админка
  клиента не увидела бы собственных статей;
* поиск — исключает демо на уровне SQL (см. `src/domain/search.py`);
* статистика — общие итоги платформы (`get_platform_stats`: просмотры и число
  выпусков на главной) демо не считают, а по-журнальные агрегаты
  (`get_journal_stats`, `get_journals_overview`, `get_journal_*` по списку id)
  считают: это цифры
  самого стенда, они нужны его публичной странице и панели клиента. Стендам
  статистику набивает `scripts/seed_demo_stats.py` — выдуманные сотни просмотров
  тем более не должны попадать в витрину.

Прямые ссылки не ломаются: `/journal/<slug>`, `/article/<slug>` и админка
продолжают отдавать демо-журнал — по этим ссылкам клиента и водят. Страницы на
фронте помечены `noindex`, чтобы демо не попало в выдачу поисковиков.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.persistence.models import Article, Issue, Journal, Profile, User

# Ключ в journals.metadata. Значение — JSON true (проверяем текстом, чтобы
# строковое "true" из ручной правки тоже считалось демо).
DEMO_FLAG = "demo"


def _flag_is_true() -> ColumnElement[bool]:
    return func.coalesce(Journal.meta[DEMO_FLAG].astext, "false") == "true"


def journal_is_not_demo() -> ColumnElement[bool]:
    """Условие на Journal: журнал не демонстрационный."""
    return func.coalesce(Journal.meta[DEMO_FLAG].astext, "false") != "true"


def demo_issue_ids() -> Select:
    """Подзапрос: id всех выпусков, принадлежащих демо-журналам."""
    return select(Issue.id).where(
        Issue.journal_id.in_(select(Journal.id).where(_flag_is_true()))
    )


def article_is_not_demo() -> ColumnElement[bool]:
    """Условие на Article: статья не из демо-журнала.

    `issue_id IS NULL` пропускаем явно: у самостоятельных изданий выпуска нет, а
    `NULL NOT IN (...)` даёт NULL и вырезал бы их из всех лент.
    """
    return or_(
        Article.issue_id.is_(None),
        Article.issue_id.notin_(demo_issue_ids()),
    )


async def article_is_demo(db: AsyncSession, article_id: int) -> bool:
    """Статья лежит в выпуске демо-журнала."""
    row = (
        await db.execute(
            select(Article.id).where(
                Article.id == article_id, Article.issue_id.in_(demo_issue_ids())
            )
        )
    ).first()
    return row is not None


# --------------------------------------------------------------------------- #
# Демо-профиль исследователя
#
# Заводится `scripts/create_demo_researcher.py`, чтобы показать кабинет
# исследователя (все кнопки владельца) дизайнеру или клиенту прямо на проде, без
# регистрации. Пометка в `profiles.metadata`:
#   {"demo": true, "demo_login": {"sha256": "...", "expires_at": "ISO"}}
# Что она делает:
# * карусель профилей на главной его не показывает (`public_profiles`);
# * страница профиля отдаётся с `is_demo`, фронт ставит ей noindex;
# * «Прикрепить публикацию» принимает только статьи демо-журналов, а импорт по
#   DOI и ORCID, привязка ORCID и заявка «Это я» закрыты — иначе кнопки,
#   которые для того и показывают, поменяли бы настоящие данные;
# * вход — по ссылке `GET /auth/demo-login?t=<токен>`; в базе только SHA-256
#   токена и срок, сам токен печатается скриптом один раз.
# --------------------------------------------------------------------------- #

DEMO_LOGIN = "demo_login"


def meta_is_demo(meta: dict[str, Any] | None) -> bool:
    return str((meta or {}).get(DEMO_FLAG)).lower() == "true"


def is_demo_profile(profile: Profile | None) -> bool:
    return profile is not None and meta_is_demo(profile.meta)


def profile_is_not_demo() -> ColumnElement[bool]:
    """Условие на Profile: профиль не демонстрационный."""
    return func.coalesce(Profile.meta[DEMO_FLAG].astext, "false") != "true"


async def profile_is_demo(db: AsyncSession, profile_id) -> bool:
    meta = (
        await db.execute(select(Profile.meta).where(Profile.id == profile_id))
    ).scalar_one_or_none()
    return meta_is_demo(meta)


def demo_login_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def demo_user_for_login(db: AsyncSession, token: str) -> User | None:
    """Пользователь демо-профиля по токену ссылки автовхода или None.

    None — и для неизвестного токена, и для просроченного, и для профиля, с
    которого сняли пометку demo: ссылка не должна пережить ни одно из этого.
    """
    if not token or len(token) < 32:
        return None
    profile = (
        await db.execute(
            select(Profile).where(
                Profile.meta[DEMO_LOGIN]["sha256"].astext == demo_login_hash(token)
            )
        )
    ).scalars().first()
    if not is_demo_profile(profile):
        return None
    raw = ((profile.meta or {}).get(DEMO_LOGIN) or {}).get("expires_at")
    try:
        expires = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        return None
    return (
        await db.execute(select(User).where(User.id == profile.id))
    ).scalars().first()

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
* статистика — общая сумма платформы (`get_platform_stats`, счётчик просмотров
  на главной) демо не считает, а по-журнальные агрегаты (`get_journal_stats`,
  `get_journals_overview`, `get_journal_*` по списку id) считают: это цифры
  самого стенда, они нужны его публичной странице и панели клиента. Стендам
  статистику набивает `scripts/seed_demo_stats.py` — выдуманные сотни просмотров
  тем более не должны попадать в витрину.

Прямые ссылки не ломаются: `/journal/<slug>`, `/article/<slug>` и админка
продолжают отдавать демо-журнал — по этим ссылкам клиента и водят. Страницы на
фронте помечены `noindex`, чтобы демо не попало в выдачу поисковиков.
"""
from __future__ import annotations

from sqlalchemy import ColumnElement, Select, func, or_, select

from src.infrastructure.persistence.models import Article, Issue, Journal

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

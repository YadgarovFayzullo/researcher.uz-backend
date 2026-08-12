"""Поиск статей — порт RPC `search_articles(q)` (Фаза 7).

RPC жил только в проде (не в `supabase/*.sql`); семантика восстановлена по
`SearchPageClient.tsx`: на вход строка `q`, на выход плоский список статей,
ранжированный по релевантности; фронт сам пагинирует (10/стр) и берёт
`totalResults = data.length`. Дату публикации RPC отдаёт как `data` (фронт
мапит в `created_at`).

Реализация: полнотекст по `search_vector` (конфиг `simple` + `unaccent`,
собран в scripts/build_search_index.py) с `websearch_to_tsquery` и `ts_rank`;
при пустом результате — триграммный fallback по заголовку (опечатки/подстроки).
Счётчики просмотров/скачиваний доклеиваются батчем (как во фронте).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.stats import StatsDomain

# Верхняя граница выдачи (фронт пагинирует клиентски по 10). При переносе фронта
# перейдём на серверную пагинацию; пока отдаём разумный максимум.
_LIMIT = 100

_FTS_SQL = text(
    """
    with q as (select websearch_to_tsquery('simple', unaccent(:q)) as tsq)
    select a.id, a.title, a.title_foreign, a.slug, a.authors,
           a.annotation, a.keywords, a.publication_type, a.publication_year,
           a.pdf, a.cover_image, a.doi,
           a.data, a.created_at,
           j.name as journal_name,
           ts_rank(a.search_vector, q.tsq) as rank
    from public.articles a
    cross join q
    left join public.issues i on i.id = a.issue_id
    left join public.journals j on j.id = i.journal_id
    where a.published is true and a.search_vector @@ q.tsq
    order by rank desc, a.data desc nulls last
    limit :lim
    """
)

# Fallback: триграммы по заголовку (+ foreign), когда FTS ничего не нашёл.
_TRGM_SQL = text(
    """
    select a.id, a.title, a.title_foreign, a.slug, a.authors,
           a.annotation, a.keywords, a.publication_type, a.publication_year,
           a.pdf, a.cover_image, a.doi,
           a.data, a.created_at,
           j.name as journal_name,
           greatest(
             word_similarity(unaccent(:q), unaccent(coalesce(a.title,''))),
             word_similarity(unaccent(:q), unaccent(coalesce(a.title_foreign,'')))
           ) as rank
    from public.articles a
    left join public.issues i on i.id = a.issue_id
    left join public.journals j on j.id = i.journal_id
    where a.published is true
      and (unaccent(:q) <% unaccent(coalesce(a.title,''))
           or unaccent(:q) <% unaccent(coalesce(a.title_foreign,'')))
    order by rank desc, a.data desc nulls last
    limit :lim
    """
)


# Семантический поиск по эмбеддингам (pgvector). 1316/1937 статей уже несут
# 768-мерный вектор (перенесены из прода). Косинусная близость: оператор `<=>`.
# ВНИМАНИЕ: чтобы искать, запрос надо векторизовать ТОЙ ЖЕ моделью, что породила
# эти 768-мерные векторы — модель в репозитории отсутствует, ждём подтверждения
# владельца (см. MIGRATION_PLAN Фаза 7). Метод готов и заработает, как только
# появится провайдер эмбеддингов запроса.
_VECTOR_SQL = text(
    """
    select a.id, a.title, a.title_foreign, a.slug, a.authors,
           a.annotation, a.keywords, a.publication_type, a.publication_year,
           a.pdf, a.cover_image, a.doi, a.data, a.created_at,
           j.name as journal_name,
           1 - (a.embedding <=> cast(:vec as vector)) as score
    from public.articles a
    left join public.issues i on i.id = a.issue_id
    left join public.journals j on j.id = i.journal_id
    where a.published is true and a.embedding is not null
    order by a.embedding <=> cast(:vec as vector)
    limit :lim
    """
)


class SearchDomain:
    async def search_by_vector(
        self, db: AsyncSession, query_vector: list[float], *, limit: int = 20
    ) -> list[dict]:
        """Семантический поиск: ближайшие статьи по косинусной близости эмбеддинга.
        `query_vector` должен быть получен той же моделью, что и хранимые (768-мер)."""
        if not query_vector:
            return []
        vec = "[" + ",".join(str(float(x)) for x in query_vector) + "]"
        rows = (
            await db.execute(_VECTOR_SQL, {"vec": vec, "lim": limit})
        ).mappings().all()
        return [dict(r) for r in rows]

    async def search_articles(
        self, db: AsyncSession, q: str, *, limit: int = _LIMIT
    ) -> list[dict]:
        q = (q or "").strip()
        if not q:
            return []

        rows = (await db.execute(_FTS_SQL, {"q": q, "lim": limit})).mappings().all()
        if not rows:
            rows = (
                await db.execute(_TRGM_SQL, {"q": q, "lim": limit})
            ).mappings().all()

        results = [dict(r) for r in rows]
        if not results:
            return []

        # Доклеиваем views/downloads батчем (как enrichedData во фронте).
        ids = [r["id"] for r in results]
        stats = {
            s["article_id"]: s
            for s in await StatsDomain.get_article_stats_batch(db, ids)
        }
        for r in results:
            s = stats.get(r["id"])
            r["views"] = s["views"] if s else 0
            r["downloads"] = s["downloads"] if s else 0
            # RPC отдавал дату как `data`; фронт мапит в created_at.
            if r.get("created_at") is None:
                r["created_at"] = r.get("data")
            r.pop("rank", None)
        return results

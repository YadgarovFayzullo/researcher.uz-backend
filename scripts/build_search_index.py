"""
Фаза 7 — полнотекстовый поиск: пересборка tsvector + триггер + GIN-индекс.

Phase 2 не переносила tsvector-колонки (document_ru/en/uz, search_vector) — они
строятся здесь. Контент мультиязычный (узбекская латиница + русский + английский),
специального узбекского стеммера в Postgres нет, поэтому основной вектор поиска —
`search_vector` на конфиге `simple` + `unaccent` (без стемминга, работает на всех
языках), с весами: заголовок = A, ключевые слова/авторы = B, аннотация = C.
document_ru/en/uz заполняются для полноты (russian/english/simple-стеммеры).

Идемпотентно (create or replace / if not exists). Запуск:
  .venv/bin/python scripts/build_search_index.py
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from src.infrastructure.persistence.db import AsyncSessionLocal, engine

# Общее выражение для основного вектора поиска (используется и в триггере, и в
# начальном populate). unaccent() — STABLE, поэтому функция помечена STABLE и
# применяется только на запись (в колонку), не в функциональном индексе.
SEARCH_TSV_FN = """
create or replace function public.article_search_tsv(
  p_title text, p_title_foreign text, p_authors text,
  p_keywords text, p_keywords_foreign text,
  p_annotation text, p_annotation_foreign text, p_field text
) returns tsvector
language sql stable
set search_path = public
as $$
  select
    setweight(to_tsvector('simple', unaccent(
      coalesce(p_title,'') || ' ' || coalesce(p_title_foreign,''))), 'A') ||
    setweight(to_tsvector('simple', unaccent(
      coalesce(p_keywords,'') || ' ' || coalesce(p_keywords_foreign,'') || ' ' ||
      coalesce(p_authors,''))), 'B') ||
    setweight(to_tsvector('simple', unaccent(
      coalesce(p_annotation,'') || ' ' || coalesce(p_annotation_foreign,'') || ' ' ||
      coalesce(p_field,''))), 'C');
$$;
"""

TRIGGER_FN = """
create or replace function public.articles_tsv_trigger()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  new.search_vector := public.article_search_tsv(
    new.title, new.title_foreign, new.authors,
    new.keywords, new.keywords_foreign,
    new.annotation, new.annotation_foreign, new.field_of_science);
  new.document_ru := to_tsvector('russian', unaccent(
    coalesce(new.title,'') || ' ' || coalesce(new.annotation,'') || ' ' ||
    coalesce(new.keywords,'')));
  new.document_en := to_tsvector('english', unaccent(
    coalesce(new.title_foreign,'') || ' ' || coalesce(new.annotation_foreign,'') || ' ' ||
    coalesce(new.keywords_foreign,'')));
  new.document_uz := to_tsvector('simple', unaccent(
    coalesce(new.title,'') || ' ' || coalesce(new.keywords,'') || ' ' ||
    coalesce(new.annotation,'')));
  return new;
end;
$$;
"""

# asyncpg не даёт несколько команд в одном prepared statement — по одной.
TRIGGER_DROP = "drop trigger if exists articles_tsv_update on public.articles;"
TRIGGER_CREATE = """
create trigger articles_tsv_update
  before insert or update of title, title_foreign, authors, keywords,
    keywords_foreign, annotation, annotation_foreign, field_of_science
  on public.articles
  for each row execute function public.articles_tsv_trigger();
"""

INDEXES = [
    "create index if not exists articles_search_vector_gin on public.articles using gin (search_vector);",
    "create index if not exists articles_document_ru_gin on public.articles using gin (document_ru);",
    "create index if not exists articles_document_en_gin on public.articles using gin (document_en);",
    "create index if not exists articles_document_uz_gin on public.articles using gin (document_uz);",
    # триграммы для fallback по опечаткам/подстрокам
    "create index if not exists articles_title_trgm on public.articles using gin (title gin_trgm_ops);",
]

# Начальный populate: тот же expr, что в триггере (гоняем UPDATE один раз).
POPULATE = """
update public.articles set
  search_vector = public.article_search_tsv(
    title, title_foreign, authors, keywords, keywords_foreign,
    annotation, annotation_foreign, field_of_science),
  document_ru = to_tsvector('russian', unaccent(
    coalesce(title,'') || ' ' || coalesce(annotation,'') || ' ' || coalesce(keywords,''))),
  document_en = to_tsvector('english', unaccent(
    coalesce(title_foreign,'') || ' ' || coalesce(annotation_foreign,'') || ' ' || coalesce(keywords_foreign,''))),
  document_uz = to_tsvector('simple', unaccent(
    coalesce(title,'') || ' ' || coalesce(keywords,'') || ' ' || coalesce(annotation,'')));
"""


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(text(SEARCH_TSV_FN))
        await conn.execute(text(TRIGGER_FN))
        await conn.execute(text(TRIGGER_DROP))
        await conn.execute(text(TRIGGER_CREATE))
        for stmt in INDEXES:
            await conn.execute(text(stmt))
        print("DDL применён (функция, триггер, индексы).")
        res = await conn.execute(text(POPULATE))
        print(f"Populate: обновлено строк ~ {res.rowcount}")

    async with AsyncSessionLocal() as db:
        r = await db.execute(text(
            "select count(*) total, count(search_vector) sv, "
            "count(*) filter (where search_vector <> '') nonempty from articles"))
        row = r.first()
        print(f"Проверка: total={row.total} search_vector={row.sv} непустых={row.nonempty}")


if __name__ == "__main__":
    asyncio.run(main())

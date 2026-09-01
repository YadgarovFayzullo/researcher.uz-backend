"""OAI-PMH провайдер: клиенты с ключами + отметка правки статьи

Revision ID: a4c81e6b2f90
Revises: f6a1c9d20b73
Create Date: 2026-09-01

Две вещи:

1. `oai_clients` — реестр харвестеров. Эндпоинт /oai закрыт: без ключа из этой
   таблицы отдаётся 401. Храним только sha256 ключа, сам ключ виден один раз
   при выдаче (scripts/oai_client.py).

2. `articles.updated_at` — датировка записи для инкрементального сбора. Без неё
   харвестер не узнаёт о правках: он спрашивает «что изменилось с прошлого
   раза», и записи с прежней датой просто не перезабирает.

   Триггер намеренно НЕ трогает дату при изменении счётчиков просмотров и
   скачиваний (`StatsDomain._bump` пишет в ту же строку на каждый просмотр) —
   иначе любой читатель делал бы статью «изменённой», и харвестеры тянули бы
   всю базу заново каждый день.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a4c81e6b2f90"
down_revision: Union[str, Sequence[str], None] = "f6a1c9d20b73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Колонки, правка которых означает новую версию библиографической записи.
# Всё остальное (счётчики, tsvector, embedding) на харвестеров не влияет.
_BIBLIO_COLUMNS = (
    "title",
    "title_foreign",
    "authors",
    "pages",
    "doi",
    "annotation",
    "annotation_foreign",
    "field_of_science",
    "keywords",
    "keywords_foreign",
    "pdf",
    "issue_id",
    "slug",
    "data",
    "published",
    "publication_type",
    "isbn",
    "publisher",
    "publication_year",
    "cover_image",
    "metadata",
    "publisher_id",
    "section_id",
)


def _row(prefix: str) -> str:
    return "(" + ", ".join(f"{prefix}.{c}" for c in _BIBLIO_COLUMNS) + ")"


def upgrade() -> None:
    op.create_table(
        "oai_clients",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        # sha256(ключ) в hex. Сам ключ нигде не хранится.
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        # Пустой массив = ограничения нет. Иначе список IP/CIDR.
        sa.Column(
            "ip_allowlist",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        # Пустой массив = все журналы. Иначе клиент видит только эти.
        sa.Column(
            "allowed_journal_ids",
            postgresql.ARRAY(sa.BigInteger()),
            server_default=sa.text("'{}'::bigint[]"),
            nullable=False,
        ),
        # Отдавать ли ссылку на PDF (партнёру по полным текстам — да,
        # метаданным-агрегаторам вроде BASE — нет).
        sa.Column(
            "include_fulltext", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_ip", sa.Text(), nullable=True),
        sa.Column(
            "requests_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="oai_clients_token_hash_key"),
    )

    op.add_column(
        "articles",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    # Существующим записям — дату создания, иначе после выкатки вся база
    # выглядела бы «изменённой сегодня».
    op.execute("UPDATE articles SET updated_at = created_at")
    # Индекс под выборку `from`/`until`: харвестер всегда режет по дате.
    op.create_index("idx_articles_updated_at", "articles", ["updated_at"])

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION articles_touch_updated_at() RETURNS trigger AS $$
        BEGIN
          IF {_row('NEW')} IS DISTINCT FROM {_row('OLD')} THEN
            NEW.updated_at := now();
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER articles_touch_updated_at
        BEFORE UPDATE ON articles
        FOR EACH ROW EXECUTE FUNCTION articles_touch_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS articles_touch_updated_at ON articles")
    op.execute("DROP FUNCTION IF EXISTS articles_touch_updated_at()")
    op.drop_index("idx_articles_updated_at", table_name="articles")
    op.drop_column("articles", "updated_at")
    op.drop_table("oai_clients")

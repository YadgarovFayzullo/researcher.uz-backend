"""Корзина статей: удалённая статья хранится 30 дней и восстанавливается целиком.

Снимок строки статьи и всех зависимых строк лежит в `snapshot` (JSONB), а не
в копиях таблиц: схема статей меняется, и копия каждой таблицы требовала бы
миграции на каждое изменение оригинала. FK на выпуск, журнал и удалившего нет
намеренно — к моменту восстановления их самих может уже не быть, а запись
корзины должна пережить что угодно.

Revision ID: a4d8e2f17c63
Revises: e3a7f51c9b28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a4d8e2f17c63"
down_revision: Union[str, Sequence[str], None] = "e3a7f51c9b28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "article_trash",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        # Копии полей для списка корзины — чтобы не разбирать snapshot на
        # каждой строке выдачи.
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("slug", sa.Text(), nullable=True),
        sa.Column("issue_id", sa.BigInteger(), nullable=True),
        sa.Column("journal_id", sa.BigInteger(), nullable=True),
        sa.Column("journal_name", sa.Text(), nullable=True),
        sa.Column("deleted_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # Очистка идёт по сроку, список — от свежих к старым.
    op.create_index("ix_article_trash_deleted_at", "article_trash", ["deleted_at"])
    op.create_index("ix_article_trash_journal_id", "article_trash", ["journal_id"])


def downgrade() -> None:
    op.drop_index("ix_article_trash_journal_id", table_name="article_trash")
    op.drop_index("ix_article_trash_deleted_at", table_name="article_trash")
    op.drop_table("article_trash")

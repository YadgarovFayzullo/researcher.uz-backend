"""Карточки авторов: таблица authors + связь из article_authors.

Почему отдельная таблица, а не строка в profiles: profiles.id — внешний ключ на
users.id, то есть профиль не существует без аккаунта. Автору импортированной
статьи аккаунт не заводили и не заведут за него — карточка должна жить сама по
себе, а claim лишь связывает её с профилем (authors.profile_id).

Revision ID: c92f4d1a7b30
Revises: b1e7d3a95c40
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c92f4d1a7b30"
down_revision: Union[str, Sequence[str], None] = "b1e7d3a95c40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "authors",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        # Ключ личности из src/domain/author_names.identity_key: фамилия и набор
        # инициалов. Уникален — он и есть «тот же человек».
        sa.Column("name_key", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        # Заполняется при claim: с этого момента карточка = профиль человека.
        sa.Column("profile_id", sa.UUID(), nullable=True),
        sa.Column("orcid", sa.Text(), nullable=True),
        # Денормализация: страниц авторов тысячи, и порог индексации (≥2 работы)
        # проверяется на каждой выдаче — считать count() каждый раз незачем.
        sa.Column(
            "works_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name_key", name="authors_name_key_key"),
        sa.UniqueConstraint("slug", name="authors_slug_key"),
    )
    # Список карточек для индексации и выдачи: сначала самые публикующиеся.
    op.create_index("ix_authors_works_count", "authors", ["works_count"])
    op.create_index("ix_authors_profile_id", "authors", ["profile_id"])

    op.add_column("article_authors", sa.Column("author_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "article_authors_author_id_fkey",
        "article_authors",
        "authors",
        ["author_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_article_authors_author_id", "article_authors", ["author_id"])


def downgrade() -> None:
    op.drop_index("ix_article_authors_author_id", table_name="article_authors")
    op.drop_constraint(
        "article_authors_author_id_fkey", "article_authors", type_="foreignkey"
    )
    op.drop_column("article_authors", "author_id")
    op.drop_index("ix_authors_profile_id", table_name="authors")
    op.drop_index("ix_authors_works_count", table_name="authors")
    op.drop_table("authors")

"""Заявки на присвоение карточки автора.

Присвоение проверяется вручную: число публикаций идёт в аттестационные
документы, то есть у присвоения чужих работ есть прямая выгода, а полагаться на
совпадение ФИО нельзя — имя в профиле правит сам пользователь.

Revision ID: d13a5c8e64b2
Revises: c92f4d1a7b30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d13a5c8e64b2"
down_revision: Union[str, Sequence[str], None] = "c92f4d1a7b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "author_claims",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("author_id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'pending'::text"), nullable=False
        ),
        # Чем заявитель подтверждает авторство: место работы, почта из статьи,
        # ссылка на профиль. Владельцу этого хватает, чтобы решить.
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.UUID(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text])",
            name="author_claims_status_check",
        ),
        sa.ForeignKeyConstraint(["author_id"], ["authors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by"], ["profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # Одна открытая заявка на пару «карточка + человек»: повторные нажатия
    # кнопки не должны плодить очередь, а отклонённую заявку можно подать снова.
    op.create_index(
        "ux_author_claims_pending",
        "author_claims",
        ["author_id", "profile_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index("ix_author_claims_status", "author_claims", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_author_claims_status", table_name="author_claims")
    op.drop_index("ux_author_claims_pending", table_name="author_claims")
    op.drop_table("author_claims")

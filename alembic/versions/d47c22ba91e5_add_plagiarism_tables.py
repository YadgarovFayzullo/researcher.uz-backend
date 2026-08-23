"""Проверка на заимствования: тексты, отпечатки, проверки

Revision ID: d47c22ba91e5
Revises: c31f7ab90d12
Create Date: 2026-08-24

Написано руками (autogenerate в этом проекте пытается снести GIN/trgm-индексы
articles, которых нет в моделях). Создаются только четыре новые таблицы.

Смысл — в `src/domain/similarity.py`: отпечаток статьи это шинглы после
winnowing, и поиск заимствований сводится к пересечению множеств хешей.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d47c22ba91e5"
down_revision: Union[str, Sequence[str], None] = "c31f7ab90d12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "article_texts",
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("words_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("shingles_total", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'ok'::text"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("article_id"),
    )

    op.create_table(
        "article_fingerprints",
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("hash", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("article_id", "hash"),
    )
    # Рабочая лошадь проверки: «какие статьи содержат эти хеши».
    op.create_index("ix_article_fingerprints_hash", "article_fingerprints", ["hash"])

    op.create_table(
        "plagiarism_checks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("article_id", sa.BigInteger(), nullable=True),
        sa.Column("journal_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'::text"), nullable=False),
        sa.Column("score", sa.Numeric(), nullable=True),
        sa.Column("words_count", sa.Integer(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status = ANY (ARRAY['pending'::text, 'running'::text, 'done'::text, "
            "'failed'::text])",
            name="plagiarism_checks_status_check",
        ),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["journal_id"], ["journals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_plagiarism_checks_journal", "plagiarism_checks", ["journal_id", "created_at"]
    )

    op.create_table(
        "plagiarism_matches",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("check_id", sa.BigInteger(), nullable=False),
        sa.Column("source_article_id", sa.BigInteger(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_title", sa.Text(), nullable=True),
        sa.Column("matched_shingles", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("score", sa.Numeric(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "fragments",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["check_id"], ["plagiarism_checks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_article_id"], ["articles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_plagiarism_matches_check", "plagiarism_matches", ["check_id", "score"])


def downgrade() -> None:
    op.drop_index("ix_plagiarism_matches_check", table_name="plagiarism_matches")
    op.drop_table("plagiarism_matches")
    op.drop_index("ix_plagiarism_checks_journal", table_name="plagiarism_checks")
    op.drop_table("plagiarism_checks")
    op.drop_index("ix_article_fingerprints_hash", table_name="article_fingerprints")
    op.drop_table("article_fingerprints")
    op.drop_table("article_texts")

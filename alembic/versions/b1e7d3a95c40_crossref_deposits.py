"""crossref_deposits: состояние регистрации DOI в Crossref

Одна строка на (статья, среда). Среда в ключе, потому что песочница
test.crossref.org — отдельный мир со своей учёткой: прогон в ней не должен
выглядеть боевой регистрацией и не должен мешать сделать её после.

Индекс по (status, submitted_at) — под опрос результатов: фоновый проход
берёт именно «отправленные, самые старые».

Revision ID: b1e7d3a95c40
Revises: a4c81e6b2f90
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b1e7d3a95c40"
down_revision = "a4c81e6b2f90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crossref_deposits",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "environment", sa.Text(), server_default=sa.text("'test'::text"), nullable=False
        ),
        # DOI фиксируется на строке, а не вычисляется на лету: шаблон суффикса
        # в настройках когда-нибудь поменяют, а выданный DOI неизменен.
        sa.Column("doi", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'pending'::text"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status = ANY (ARRAY['pending'::text, 'submitted'::text, "
            "'registered'::text, 'failed'::text])",
            name="crossref_deposits_status_check",
        ),
        sa.CheckConstraint(
            "environment = ANY (ARRAY['test'::text, 'production'::text])",
            name="crossref_deposits_environment_check",
        ),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "article_id", "environment", name="crossref_deposits_article_env_key"
        ),
        sa.UniqueConstraint("batch_id", name="crossref_deposits_batch_id_key"),
    )
    op.create_index(
        "ix_crossref_deposits_status", "crossref_deposits", ["status", "submitted_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_crossref_deposits_status", table_name="crossref_deposits")
    op.drop_table("crossref_deposits")

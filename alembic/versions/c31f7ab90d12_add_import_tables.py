"""import_jobs + import_items (импорт архивов с других платформ)

Revision ID: c31f7ab90d12
Revises: bad5bb45ae32
Create Date: 2026-08-22

Ревизия написана руками, а не autogenerate: у autogenerate в этом проекте
устойчивый дрейф (пытается снести GIN/trgm-индексы articles, которые живут в
БД, но не описаны в моделях). Здесь создаются ТОЛЬКО две новые таблицы.

Смысл таблиц — в import-integration.md (репозиторий фронта): импорт не пишет в
articles напрямую, сначала строки источника оседают в import_items, клиент
смотрит превью, и только потом задача применяется.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c31f7ab90d12"
down_revision: Union[str, Sequence[str], None] = "bad5bb45ae32"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "import_jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("journal_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'draft'::text"), nullable=False
        ),
        sa.Column(
            "totals",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source_type = ANY (ARRAY['table'::text, 'oai'::text])",
            name="import_jobs_source_type_check",
        ),
        sa.CheckConstraint(
            "status = ANY (ARRAY['draft'::text, 'parsing'::text, 'ready'::text, "
            "'applying'::text, 'done'::text, 'failed'::text, 'cancelled'::text])",
            name="import_jobs_status_check",
        ),
        sa.ForeignKeyConstraint(["journal_id"], ["journals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_import_jobs_journal", "import_jobs", ["journal_id", "created_at"]
    )

    op.create_table(
        "import_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "parsed",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("issue_key", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'pending'::text"),
            nullable=False,
        ),
        sa.Column(
            "problems",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("article_id", sa.BigInteger(), nullable=True),
        sa.Column("pdf_source", sa.Text(), nullable=True),
        sa.Column("pdf_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status = ANY (ARRAY['pending'::text, 'duplicate'::text, 'invalid'::text, "
            "'skipped'::text, 'created'::text, 'failed'::text])",
            name="import_items_status_check",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["import_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "source_key", name="import_items_job_source_key"),
    )
    op.create_index(
        "ix_import_items_job_status", "import_items", ["job_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_import_items_job_status", table_name="import_items")
    op.drop_table("import_items")
    op.drop_index("ix_import_jobs_journal", table_name="import_jobs")
    op.drop_table("import_jobs")

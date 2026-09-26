"""conference_sessions: онлайн-сессии конференций (комнаты видеосвязи)

Организатор заводит к сборнику расписание онлайн-сессий, участники входят по
токену на meet.researcher.uz. Таблица — только расписание и слаг комнаты;
состояние самой комнаты живёт в Durable Object на Cloudflare.

Revision ID: c7e2a9f4b1d3
Revises: e9d4b1c7a305
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c7e2a9f4b1d3"
down_revision = "e9d4b1c7a305"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conference_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "issue_id",
            sa.BigInteger(),
            sa.ForeignKey("issues.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "section_id",
            sa.BigInteger(),
            sa.ForeignKey("conference_sections.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("room", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'scheduled'::text"),
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("profiles.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status = ANY (ARRAY['scheduled'::text, 'cancelled'::text])",
            name="conference_sessions_status_check",
        ),
    )
    # Публичная страница сборника и админка читают сессии по сборнику.
    op.create_index(
        "conference_sessions_issue_idx",
        "conference_sessions",
        ["issue_id", "starts_at"],
    )


def downgrade() -> None:
    op.drop_index("conference_sessions_issue_idx", table_name="conference_sessions")
    op.drop_table("conference_sessions")

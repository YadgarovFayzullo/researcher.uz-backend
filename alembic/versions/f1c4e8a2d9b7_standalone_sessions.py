"""conference_sessions: самостоятельные сессии и тумблер зала ожидания

issue_id становится nullable: сессию можно запланировать без серии и сборника
(«запланировать встречу», как в Zoom); права у создателя и владельца.
waiting_room — включён ли зал ожидания.

Revision ID: f1c4e8a2d9b7
Revises: e5a9c7d3b2f1
"""
from alembic import op
import sqlalchemy as sa

revision = "f1c4e8a2d9b7"
down_revision = "e5a9c7d3b2f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("conference_sessions", "issue_id", nullable=True)
    op.add_column(
        "conference_sessions",
        sa.Column("waiting_room", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    # Список «мои сессии» читается по создателю.
    op.create_index(
        "conference_sessions_created_by_idx", "conference_sessions", ["created_by"]
    )


def downgrade() -> None:
    op.drop_index("conference_sessions_created_by_idx", table_name="conference_sessions")
    op.drop_column("conference_sessions", "waiting_room")
    op.execute("DELETE FROM conference_sessions WHERE issue_id IS NULL")
    op.alter_column("conference_sessions", "issue_id", nullable=False)

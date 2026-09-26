"""conference_sessions: запись сессии (recording_url, size, uploaded_at)

Организатор записывает сессию в своём браузере и загружает файл в R2 напрямую
по подписанному URL — через API такой объём не прогнать. Здесь только ссылка.

Revision ID: d8f3b2c1a7e4
Revises: c7e2a9f4b1d3
"""
from alembic import op
import sqlalchemy as sa

revision = "d8f3b2c1a7e4"
down_revision = "c7e2a9f4b1d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conference_sessions", sa.Column("recording_url", sa.Text(), nullable=True))
    op.add_column("conference_sessions", sa.Column("recording_size", sa.BigInteger(), nullable=True))
    op.add_column(
        "conference_sessions",
        sa.Column("recording_uploaded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conference_sessions", "recording_uploaded_at")
    op.drop_column("conference_sessions", "recording_size")
    op.drop_column("conference_sessions", "recording_url")

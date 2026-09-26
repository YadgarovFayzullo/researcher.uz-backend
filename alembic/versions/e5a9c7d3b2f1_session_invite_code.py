"""conference_sessions.invite_code — код прямой ссылки на комнату

Прямая ссылка /uz/live/<code>, как ссылка приглашения в Zoom: с аккаунтом
человек входит сразу, без аккаунта представляется и ждёт допуска организатора.
Существующим сессиям код генерируется здесь же.

Revision ID: e5a9c7d3b2f1
Revises: d8f3b2c1a7e4
"""
from alembic import op
import sqlalchemy as sa

revision = "e5a9c7d3b2f1"
down_revision = "d8f3b2c1a7e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conference_sessions", sa.Column("invite_code", sa.Text(), nullable=True))
    op.execute(
        "UPDATE conference_sessions SET invite_code = substr(md5(random()::text || id::text), 1, 12)"
    )
    op.alter_column("conference_sessions", "invite_code", nullable=False)
    op.create_unique_constraint(
        "conference_sessions_invite_code_key", "conference_sessions", ["invite_code"]
    )


def downgrade() -> None:
    op.drop_constraint("conference_sessions_invite_code_key", "conference_sessions")
    op.drop_column("conference_sessions", "invite_code")

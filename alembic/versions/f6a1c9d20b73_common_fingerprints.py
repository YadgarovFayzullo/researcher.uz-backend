"""Стоп-лист шаблонных фраз (колонтитулы, клише)

Revision ID: f6a1c9d20b73
Revises: e58d3fc07a44
Create Date: 2026-08-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f6a1c9d20b73"
down_revision: Union[str, Sequence[str], None] = "e58d3fc07a44"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "common_fingerprints",
        sa.Column("hash", sa.BigInteger(), nullable=False),
        sa.Column("articles_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("hash"),
    )


def downgrade() -> None:
    op.drop_table("common_fingerprints")

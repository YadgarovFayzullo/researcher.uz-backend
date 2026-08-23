"""Подсветка заимствований: текст документа и интервалы совпадений

Revision ID: e58d3fc07a44
Revises: d47c22ba91e5
Create Date: 2026-08-23

Отчёт показывает документ целиком и закрашивает заимствованные куски, поэтому
нужен сам текст (файл после проверки не хранится) и границы совпадений в
символах.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e58d3fc07a44"
down_revision: Union[str, Sequence[str], None] = "d47c22ba91e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("plagiarism_checks", sa.Column("content", sa.Text(), nullable=True))
    op.add_column(
        "plagiarism_matches",
        sa.Column(
            "spans",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("plagiarism_matches", "spans")
    op.drop_column("plagiarism_checks", "content")

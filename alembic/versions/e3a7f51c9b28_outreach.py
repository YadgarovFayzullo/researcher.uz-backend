"""Рассылка авторам: журнал отправок и стоп-лист отписавшихся.

Адреса взяты из PDF статей, согласия на письма у людей нет, поэтому отписка
обязана работать с первого письма и навсегда: адрес из стоп-листа не получает
больше ничего, в какую бы кампанию его ни включили.

Revision ID: e3a7f51c9b28
Revises: d13a5c8e64b2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e3a7f51c9b28"
down_revision: Union[str, Sequence[str], None] = "d13a5c8e64b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "outreach_suppressions",
        # Адрес в нижнем регистре — ключ сравнения при отправке.
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("email"),
    )
    op.create_table(
        "outreach_sends",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("campaign", sa.Text(), nullable=False),
        sa.Column("author_slug", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        # sent | failed. Упавшую отправку можно повторить: уникальность ниже
        # проверяется скриптом только по sent.
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Одно доставленное письмо кампании на адрес — повторный запуск волны не
    # должен слать его второй раз.
    op.create_index(
        "ux_outreach_sends_sent",
        "outreach_sends",
        ["email", "campaign"],
        unique=True,
        postgresql_where=sa.text("status = 'sent'"),
    )
    op.create_index("ix_outreach_sends_slug", "outreach_sends", ["author_slug"])


def downgrade() -> None:
    op.drop_index("ix_outreach_sends_slug", table_name="outreach_sends")
    op.drop_index("ux_outreach_sends_sent", table_name="outreach_sends")
    op.drop_table("outreach_sends")
    op.drop_table("outreach_suppressions")

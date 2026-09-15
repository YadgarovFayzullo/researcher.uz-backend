"""article_authors: метка строки, заведённой кабинетом («Это я»)

Подпись из статьи и строка, которую дописал claim, — разные вещи, а отличить их
было нечем. `decide_claim` пытался опознать вторую по `author_id IS NULL`, но
`scripts/backfill_authors.py` проставляет `author_id` всем строкам подряд, и
после первого же его прогона признак переставал работать: у человека оставались
две подписи под одной статьёй и две карточки автора.

Revision ID: c7d914ba62f8
Revises: f5b2c8d41e07
"""
from alembic import op
import sqlalchemy as sa

revision = "c7d914ba62f8"
down_revision = "f5b2c8d41e07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "article_authors",
        sa.Column(
            "from_claim",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Существующие строки кабинета: их видно по тому, что человек привязан
    # (profile_id), а имя в подписи — из профиля, а не из статьи. Точную
    # разметку делает scripts/merge_claim_signatures.py, здесь только
    # безопасное умолчание: всё, что есть сейчас, считаем подписью статьи.


def downgrade() -> None:
    op.drop_column("article_authors", "from_claim")

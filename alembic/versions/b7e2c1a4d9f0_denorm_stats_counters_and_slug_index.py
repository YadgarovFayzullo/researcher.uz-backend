"""denormalized view/download counters + articles.slug index

Tier-1 оптимизация: чтобы горячие чтения статистики не агрегировали
article_interactions на каждый запрос, заводим денормализованные счётчики
articles.views_count / downloads_count и разово бэкфиллим их из лога. Плюс
индекс на articles.slug — самый частый публичный lookup (страница статьи, PDF).

Инкремент счётчиков — в домене stats (в той же транзакции, что и вставка
взаимодействия). Лог остаётся источником истины и для дедупа/аналитики.

Revision ID: b7e2c1a4d9f0
Revises: 2fd04aeedf1b
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7e2c1a4d9f0"
down_revision: Union[str, Sequence[str], None] = "2fd04aeedf1b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "articles",
        sa.Column("views_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "articles",
        sa.Column("downloads_count", sa.Integer(), server_default="0", nullable=False),
    )

    # Разовый бэкфилл из текущего лога взаимодействий.
    op.execute(
        """
        UPDATE articles a SET
            views_count = COALESCE(s.v, 0),
            downloads_count = COALESCE(s.d, 0)
        FROM (
            SELECT article_id,
                   SUM(CASE WHEN view = 1 THEN 1 ELSE 0 END) AS v,
                   SUM(CASE WHEN download = 1 THEN 1 ELSE 0 END) AS d
            FROM article_interactions
            GROUP BY article_id
        ) s
        WHERE s.article_id = a.id
        """
    )

    # Индекс на slug — get_article_by_slug дергается на каждой странице/PDF.
    # Не unique: приложение и так генерит уникальный slug, а unique рискует
    # упасть на легаси-дублях; для скорости lookup достаточно btree.
    op.create_index("ix_articles_slug", "articles", ["slug"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_articles_slug", table_name="articles")
    op.drop_column("articles", "downloads_count")
    op.drop_column("articles", "views_count")

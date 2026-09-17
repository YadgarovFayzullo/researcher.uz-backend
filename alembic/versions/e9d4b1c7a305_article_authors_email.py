"""article_authors.email: почта автора, вынутая из текста статьи

Адреса авторов лежат в самих PDF — в блоке «Сведения об авторах» в конце статьи
(адрес, e-mail, иногда ORCID). Раньше их держали отдельным CSV, не связанным со
статьями, и после каждого импорта список приходилось собирать заново.

Revision ID: e9d4b1c7a305
Revises: d4b8f1a6c2e9
"""
from alembic import op
import sqlalchemy as sa

revision = "e9d4b1c7a305"
down_revision = "d4b8f1a6c2e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("article_authors", sa.Column("email", sa.Text(), nullable=True))
    # Рассылка отбирает адреса и проверяет, писали ли уже на этот ящик, —
    # оба запроса идут по нормализованному адресу.
    op.create_index(
        "article_authors_email_idx",
        "article_authors",
        [sa.text("lower(email)")],
        postgresql_where=sa.text("email IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("article_authors_email_idx", table_name="article_authors")
    op.drop_column("article_authors", "email")

"""import_jobs: источник «папка PDF» (загрузка редактором выпуска)

Revision ID: f5b2c8d41e07
Revises: a4d8e2f17c63
Create Date: 2026-09-14
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f5b2c8d41e07"
down_revision: Union[str, Sequence[str], None] = "a4d8e2f17c63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("import_jobs_source_type_check", "import_jobs", type_="check")
    op.create_check_constraint(
        "import_jobs_source_type_check",
        "import_jobs",
        "source_type = ANY (ARRAY['table'::text, 'oai'::text, 'folder'::text])",
    )


def downgrade() -> None:
    # Задачи-папки без нового значения в CHECK не пережили бы откат. Статьи,
    # созданные ими, остаются: import_items.article_id — ON DELETE SET NULL
    # со стороны статьи, а сами статьи на задачу не ссылаются.
    op.execute("DELETE FROM import_jobs WHERE source_type = 'folder'")
    op.drop_constraint("import_jobs_source_type_check", "import_jobs", type_="check")
    op.create_check_constraint(
        "import_jobs_source_type_check",
        "import_jobs",
        "source_type = ANY (ARRAY['table'::text, 'oai'::text])",
    )

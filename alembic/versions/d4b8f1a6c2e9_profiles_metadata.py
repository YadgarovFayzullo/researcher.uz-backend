"""profiles.metadata: служебные пометки профиля (демо-профиль исследователя)

Демо-профиль заводится, чтобы показать кабинет исследователя дизайнеру или
клиенту прямо на проде — со всеми кнопками владельца, без регистрации. Пометка
`metadata.demo = true` прячет его из карусели на главной, закрывает страницу от
индекса и не даёт кнопкам кабинета тронуть настоящие данные. Там же лежит хеш
токена ссылки автовхода и её срок. Устроено по образцу `journals.metadata.demo`
(см. src/domain/demo.py), отдельных колонок под это не заводим.

Revision ID: d4b8f1a6c2e9
Revises: c7d914ba62f8
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d4b8f1a6c2e9"
down_revision = "c7d914ba62f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "profiles",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("profiles", "metadata")

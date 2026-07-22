"""add news table

Revision ID: bad5bb45ae32
Revises: b7e2c1a4d9f0
Create Date: 2026-07-22 14:59:33.470768

Autogenerate-дрейф (drop GIN/trgm-индексов articles — они живут в БД, но не
описаны в моделях) вычищен вручную: ревизия создаёт ТОЛЬКО таблицу news.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'bad5bb45ae32'
down_revision: Union[str, Sequence[str], None] = 'b7e2c1a4d9f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('news',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('slug', sa.Text(), nullable=False),
    sa.Column('excerpt', sa.Text(), nullable=True),
    sa.Column('body_html', sa.Text(), server_default=sa.text("''::text"), nullable=False),
    sa.Column('cover_image', sa.Text(), nullable=True),
    sa.Column('lang', sa.Text(), server_default=sa.text("'ru'::text"), nullable=False),
    sa.Column('status', sa.Text(), server_default=sa.text("'draft'::text"), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('admin_id', sa.UUID(), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.CheckConstraint("lang = ANY (ARRAY['ru'::text, 'uz'::text, 'en'::text])", name='news_lang_check'),
    sa.CheckConstraint("status = ANY (ARRAY['draft'::text, 'published'::text])", name='news_status_check'),
    sa.ForeignKeyConstraint(['admin_id'], ['profiles.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_news_feed', 'news', ['status', 'published_at'], unique=False)
    op.create_index(op.f('ix_news_slug'), 'news', ['slug'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_news_slug'), table_name='news')
    op.drop_index('ix_news_feed', table_name='news')
    op.drop_table('news')

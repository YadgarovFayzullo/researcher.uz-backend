"""SQLAlchemy models — 1:1 со `schema.sql` (researcher-uz).

Источник истины — /Users/fayulloyadgarov/researcher-uz/schema.sql.
`auth.users` (Supabase) заменён собственной таблицей `users`; все FK, что
раньше ссылались на `auth.users(id)`, теперь ссылаются на `users.id`.

Важно про data-миграцию (Фаза 2): id-колонки — GENERATED ALWAYS AS IDENTITY.
После COPY данных с явными id нужно перезапустить sequence
(`ALTER TABLE ... ALTER COLUMN id RESTART WITH <max+1>`).

Замечание: атрибут `metadata` зарезервирован в SQLAlchemy Declarative
(`Base.metadata`), поэтому колонка `metadata` маппится на атрибут `meta`.
"""

import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from pgvector.sqlalchemy import Vector

from src.infrastructure.persistence.db import Base

# Регулярка ORCID (совпадает с CHECK в schema.sql: profiles.orcid_id, article_authors.orcid)
ORCID_REGEX = r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$"

# Допустимые типы публикаций (articles.publication_type)
PUBLICATION_TYPES = (
    "article",
    "monograph",
    "dissertation",
    "textbook",
    "methodical",
    "book",
    "popular",
    "conference_paper",
)

# Допустимые типы контейнеров верхнего уровня (journals.type)
JOURNAL_TYPES = ("journal", "conference_series")


def _now_utc():
    """server_default = timezone('utc', now()) — как в schema.sql."""
    return text("timezone('utc'::text, now())")


# ---------------------------------------------------------------------------
# Auth (замена Supabase auth.users)
# ---------------------------------------------------------------------------

class User(Base):
    """Замена Supabase `auth.users`. Пароль nullable — у OAuth-юзеров пусто."""

    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(Text, unique=True, nullable=True)
    password_hash = Column(Text, nullable=True)  # bcrypt $2a$ (перенос из auth.users)
    email_confirmed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    profile = relationship("Profile", back_populates="user", uselist=False)
    identities = relationship("Identity_", back_populates="user")


class Identity_(Base):
    """Замена `auth.identities` — связь юзера с OAuth-провайдером.

    provider: 'google' | 'email' | 'orcid'. Позволяет несколько провайдеров
    на одного пользователя.
    """

    __tablename__ = "identities"
    __table_args__ = (
        UniqueConstraint("provider", "provider_id", name="identities_provider_uq"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    provider = Column(Text, nullable=False)
    provider_id = Column(Text, nullable=False)  # google 'sub' / email / orcid id
    identity_data = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="identities")


# ---------------------------------------------------------------------------
# Профиль пользователя и связанные справочники
# ---------------------------------------------------------------------------

class Profile(Base):
    __tablename__ = "profiles"
    __table_args__ = (
        CheckConstraint(
            f"orcid_id IS NULL OR orcid_id ~ '{ORCID_REGEX}'",
            name="profiles_orcid_id_check",
        ),
    )

    id = Column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    full_name = Column(Text, nullable=True)
    avatar_url = Column(Text, nullable=True)
    role = Column(Text, server_default=text("'authenticated'::text"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    username = Column(String, unique=True, nullable=True)
    bio = Column(Text, nullable=True)
    position = Column(String, nullable=True)
    location = Column(String, nullable=True)
    website = Column(String, nullable=True)
    is_public = Column(Boolean, server_default=text("true"))
    updated_at = Column(DateTime(timezone=True), server_default=_now_utc())
    orcid_id = Column(Text, nullable=True)
    workplace = Column(Text, nullable=True)
    country = Column(Text, nullable=True)
    education = Column(Text, nullable=True)

    user = relationship("User", back_populates="profile")


class Affiliation(Base):
    __tablename__ = "affiliations"

    id = Column(BigInteger, Identity(always=True), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    organization_name = Column(String, nullable=False)
    department = Column(String, nullable=True)
    position = Column(String, nullable=True)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    is_current = Column(Boolean, server_default=text("false"))
    country = Column(String, nullable=True)
    city = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=_now_utc())


class ResearchInterest(Base):
    __tablename__ = "research_interests"

    id = Column(BigInteger, Identity(always=True), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    topic = Column(String, nullable=False)
    category = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=_now_utc())


class SocialLink(Base):
    __tablename__ = "social_links"

    id = Column(BigInteger, Identity(always=True), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    platform = Column(String, nullable=False)
    identifier = Column(String, nullable=False)
    url = Column(String, nullable=True)
    is_verified = Column(Boolean, server_default=text("false"))
    created_at = Column(DateTime(timezone=True), server_default=_now_utc())


class Achievement(Base):
    __tablename__ = "achievements"

    id = Column(BigInteger, Identity(always=True), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    organization = Column(String, nullable=True)
    date_received = Column(Date, nullable=True)
    achievement_type = Column(String, nullable=True)
    amount = Column(Numeric, nullable=True)
    currency = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=_now_utc())


class ProfileStats(Base):
    __tablename__ = "profile_stats"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    total_publications = Column(Integer, server_default=text("0"))
    total_citations = Column(Integer, server_default=text("0"))
    h_index = Column(Integer, server_default=text("0"))
    profile_views = Column(Integer, server_default=text("0"))
    last_updated = Column(DateTime(timezone=True), server_default=_now_utc())


# ---------------------------------------------------------------------------
# Издатели (публикации нестандартных типов) и конференции
# ---------------------------------------------------------------------------

class Publisher(Base):
    __tablename__ = "publishers"
    __table_args__ = (
        CheckConstraint(
            r"slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name="publishers_slug_check",
        ),
    )

    id = Column(BigInteger, Identity(always=True), primary_key=True)
    slug = Column(Text, nullable=False, unique=True)
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    logo = Column(Text, nullable=True)
    website = Column(Text, nullable=True)
    admin_id = Column(UUID(as_uuid=True), ForeignKey("profiles.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ConferenceSection(Base):
    __tablename__ = "conference_sections"

    id = Column(BigInteger, Identity(always=True), primary_key=True)
    issue_id = Column(BigInteger, ForeignKey("issues.id"), nullable=False)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    position = Column(Integer, nullable=False, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    issue = relationship("Issue", back_populates="sections")


# ---------------------------------------------------------------------------
# Иерархия контента: journals -> issues -> articles
# ---------------------------------------------------------------------------

class Journal(Base):
    __tablename__ = "journals"
    __table_args__ = (
        CheckConstraint(
            "type = ANY (ARRAY['journal'::text, 'conference_series'::text])",
            name="journals_type_check",
        ),
    )

    id = Column(BigInteger, Identity(always=True), primary_key=True)
    name = Column(Text, nullable=True)
    site_link = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    issn = Column(Text, nullable=True)
    vak = Column(Text, nullable=True)  # в БД это text, не boolean
    google_scholar = Column(Text, nullable=True)
    slug = Column(Text, nullable=False, server_default=text("''::text"))
    cover_image = Column(Text, nullable=True)
    admin_id = Column(UUID(as_uuid=True), ForeignKey("profiles.id"), nullable=True)
    printed_issn = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    theme = Column(Text, nullable=True)
    publisher = Column(Text, nullable=True)
    logo = Column(Text, nullable=True)
    subject_codes = Column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    type = Column(
        Text, nullable=False, server_default=text("'journal'::text")
    )
    meta = Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    issues = relationship("Issue", back_populates="journal")


class Issue(Base):
    __tablename__ = "issues"

    id = Column(BigInteger, Identity(always=True), primary_key=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    year = Column(BigInteger, nullable=True)
    volume = Column(Text, nullable=True)
    issue = Column(Text, nullable=True)
    journal_id = Column(BigInteger, ForeignKey("journals.id"), nullable=True)
    full_pdf = Column(Text, nullable=True)
    title = Column(Text, nullable=True)
    date_start = Column(Date, nullable=True)
    date_end = Column(Date, nullable=True)
    location = Column(Text, nullable=True)
    isbn = Column(Text, nullable=True)
    cover_image = Column(Text, nullable=True)
    meta = Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    journal = relationship("Journal", back_populates="issues")
    articles = relationship("Article", back_populates="issue")
    sections = relationship("ConferenceSection", back_populates="issue")


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        CheckConstraint("data <= CURRENT_DATE", name="articles_data_check"),
        CheckConstraint(
            "publication_type = ANY (ARRAY["
            "'article'::text, 'monograph'::text, 'dissertation'::text, "
            "'textbook'::text, 'methodical'::text, 'book'::text, "
            "'popular'::text, 'conference_paper'::text])",
            name="articles_publication_type_check",
        ),
    )

    id = Column(BigInteger, Identity(always=True), primary_key=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    title = Column(Text, nullable=True)
    title_foreign = Column(Text, nullable=True)
    authors = Column(Text, nullable=True)
    pages = Column(Text, nullable=True)
    doi = Column(Text, nullable=True)

    annotation = Column(Text, nullable=True)
    annotation_foreign = Column(Text, nullable=True)
    field_of_science = Column(Text, nullable=True)
    keywords = Column(Text, nullable=True)
    keywords_foreign = Column(Text, nullable=True)

    pdf = Column(Text, nullable=True)
    issue_id = Column(BigInteger, ForeignKey("issues.id"), nullable=True)
    slug = Column(Text, nullable=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("profiles.id"), nullable=True)

    data = Column(Date, server_default=text("CURRENT_DATE"))

    # Полнотекстовый поиск (Фаза 7)
    document_ru = Column(TSVECTOR, nullable=True)
    document_en = Column(TSVECTOR, nullable=True)
    document_uz = Column(TSVECTOR, nullable=True)
    search_vector = Column(TSVECTOR, nullable=True)

    published = Column(Boolean, server_default=text("false"))

    # Денормализованные счётчики (инкрементятся при вставке взаимодействия),
    # чтобы горячие чтения статистики не агрегировали article_interactions.
    # Источник истины — сам лог; эти колонки держатся в синхроне записью.
    views_count = Column(Integer, nullable=False, server_default=text("0"))
    downloads_count = Column(Integer, nullable=False, server_default=text("0"))

    # Семантический поиск (Фаза 7) — размерность в проде не задана, тип vector без dim
    embedding = Column(Vector(), nullable=True)

    publication_type = Column(
        Text, nullable=False, server_default=text("'article'::text")
    )
    isbn = Column(Text, nullable=True)
    publisher = Column(Text, nullable=True)  # внешний издатель (free-text)
    publication_year = Column(Integer, nullable=True)
    cover_image = Column(Text, nullable=True)
    meta = Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    admin_id = Column(UUID(as_uuid=True), ForeignKey("profiles.id"), nullable=True)
    publisher_id = Column(BigInteger, ForeignKey("publishers.id"), nullable=True)
    section_id = Column(
        BigInteger, ForeignKey("conference_sections.id"), nullable=True
    )

    issue = relationship("Issue", back_populates="articles")
    interactions = relationship("ArticleInteraction", back_populates="article")
    authors_rel = relationship("ArticleAuthor", back_populates="article")


# ---------------------------------------------------------------------------
# Взаимодействия, авторы, сохранённое, цитирования
# ---------------------------------------------------------------------------

class ArticleInteraction(Base):
    """Отслеживание взаимодействий (просмотры, скачивания, лайки)."""

    __tablename__ = "article_interactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    article_id = Column(BigInteger, ForeignKey("articles.id"), nullable=True)
    ip_address = Column(INET, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=_now_utc())

    view = Column(Numeric, nullable=True)
    download = Column(Numeric, nullable=True)
    like = Column(Numeric, nullable=True)
    dislike = Column(Numeric, nullable=True)

    article = relationship("Article", back_populates="interactions")


class ArticleAuthor(Base):
    __tablename__ = "article_authors"
    __table_args__ = (
        CheckConstraint(
            f"orcid IS NULL OR orcid ~ '{ORCID_REGEX}'",
            name="article_authors_orcid_check",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # В schema.sql article_id объявлен integer (не bigint) — сохраняем как есть.
    article_id = Column(Integer, ForeignKey("articles.id"), nullable=False)
    profile_id = Column(UUID(as_uuid=True), ForeignKey("profiles.id"), nullable=True)
    author_order = Column(Integer, nullable=False)
    author_name = Column(Text, nullable=False)
    is_verified = Column(Boolean, server_default=text("false"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
    orcid = Column(Text, nullable=True)

    article = relationship("Article", back_populates="authors_rel")


class SavedArticle(Base):
    __tablename__ = "saved_articles"

    id = Column(BigInteger, Identity(always=True), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    article_id = Column(BigInteger, ForeignKey("articles.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class JournalAdmin(Base):
    """Привязка админов к журналам (authz)."""

    __tablename__ = "journal_admins"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    journal_id = Column(BigInteger, ForeignKey("journals.id"), primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # В schema.sql колонка называется "Journal name" (с пробелом) — денормализация.
    journal_name = Column("Journal name", Text, nullable=True)


class ResearcherWork(Base):
    """Работы исследователя, импортированные из ORCID."""

    __tablename__ = "researcher_works"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    orcid = Column(Text, nullable=False)
    put_code = Column(Text, nullable=False)
    title = Column(Text, nullable=True)
    work_type = Column(Text, nullable=True)
    year = Column(Integer, nullable=True)
    doi = Column(Text, nullable=True)
    url = Column(Text, nullable=True)
    container = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class ArticleReference(Base):
    """Пристатейные ссылки (references)."""

    __tablename__ = "article_references"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    article_id = Column(BigInteger, ForeignKey("articles.id"), nullable=False)
    cited_article_id = Column(BigInteger, ForeignKey("articles.id"), nullable=True)
    cited_doi = Column(Text, nullable=True)
    raw = Column(Text, nullable=True)
    position = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ExternalCitation(Base):
    """Внешние цитирования (OpenAlex и т.п.), 1 строка на статью."""

    __tablename__ = "external_citations"

    article_id = Column(BigInteger, ForeignKey("articles.id"), primary_key=True)
    doi = Column(Text, nullable=True)
    cited_by_count = Column(Integer, nullable=False, server_default=text("0"))
    counts_by_year = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    source = Column(Text, nullable=False, server_default=text("'openalex'::text"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

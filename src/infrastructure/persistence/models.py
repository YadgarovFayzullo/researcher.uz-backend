from sqlalchemy import (
    Column, BigInteger, Text, ForeignKey, 
    DateTime, Date, Boolean, Numeric
)
from sqlalchemy.dialects.postgresql import UUID, INET, TSVECTOR
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from src.infrastructure.persistence.db import Base
import uuid


class Journal(Base):
    __tablename__ = "journals"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=True)
    slug = Column(Text, nullable=False, default='')
    site_link = Column(Text, nullable=True)
    issn = Column(Text, nullable=True)
    printed_issn = Column(Text, nullable=True)
    vak = Column(Text, nullable=True)  # В БД это text, не boolean
    google_scholar = Column(Text, nullable=True)
    cover_image = Column(Text, nullable=True)
    admin_id = Column(UUID(as_uuid=True), nullable=True)
    description = Column(Text, nullable=True)
    theme = Column(Text, nullable=True)
    publisher = Column(Text, nullable=True)
    logo = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    issues = relationship("Issue", back_populates="journal")


class Issue(Base):
    __tablename__ = "issues"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    year = Column(BigInteger, nullable=True)
    volume = Column(Text, nullable=True)
    issue = Column(Text, nullable=True)
    journal_id = Column(BigInteger, ForeignKey("journals.id"), nullable=True)
    
    # Relationships
    journal = relationship("Journal", back_populates="issues")
    articles = relationship("Article", back_populates="issue")


class Article(Base):
    __tablename__ = "articles"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    title = Column(Text, nullable=True)
    title_foreign = Column(Text, nullable=True)
    
    slug = Column(Text, nullable=True)
    
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
    user_id = Column(UUID(as_uuid=True), nullable=True)
    
    data = Column(Date, nullable=True)  # В БД поле называется 'data', не 'date'
    
    # Полнотекстовый поиск (tsvector)
    document_ru = Column(TSVECTOR, nullable=True)
    document_en = Column(TSVECTOR, nullable=True)
    document_uz = Column(TSVECTOR, nullable=True)
    search_vector = Column(TSVECTOR, nullable=True)
    
    published = Column(Boolean, default=False)
    
    # Relationships
    issue = relationship("Issue", back_populates="articles")
    interactions = relationship("ArticleInteraction", back_populates="article")


class ArticleInteraction(Base):
    """Таблица для отслеживания взаимодействий с статьями (просмотры, лайки и т.д.)"""
    __tablename__ = "article_interactions"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    article_id = Column(BigInteger, ForeignKey("articles.id"), nullable=True)
    ip_address = Column(INET, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    view = Column(Numeric, nullable=True)
    download = Column(Numeric, nullable=True)
    like = Column(Numeric, nullable=True)
    dislike = Column(Numeric, nullable=True)
    
    # Relationships
    article = relationship("Article", back_populates="interactions")
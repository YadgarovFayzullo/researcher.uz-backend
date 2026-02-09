from pydantic import BaseModel, ConfigDict
from uuid import UUID
from datetime import datetime


class IDMixin(BaseModel):
    id: int


class JournalBase(BaseModel):
    name: str = ""
    site_link: str | None = None
    issn: str | None = None
    printed_issn: str | None = None
    vak: str | None = None  # В базе это text
    google_scholar: str | None = None
    cover_image: str | None = None
    description: str | None = None
    theme: str | None = None
    publisher: str | None = None
    logo: str | None = None
    
    
class JournalCreate(JournalBase):
    slug: str


class JournalUpdate(BaseModel):
    name: str | None = None
    site_link: str | None = None
    issn: str | None = None
    printed_issn: str | None = None
    vak: str | None = None
    google_scholar: str | None = None
    cover_image: str | None = None
    description: str | None = None
    theme: str | None = None
    publisher: str | None = None
    logo: str | None = None


class JournalPublic(JournalBase, IDMixin):
    model_config = ConfigDict(from_attributes=True)
    slug: str
    created_at: datetime | None = None
    admin_id: UUID | None = None


class JournalAdmin(JournalPublic):
    pass
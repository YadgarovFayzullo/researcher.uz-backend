import datetime
from pydantic import BaseModel, ConfigDict, field_validator, Field, AliasChoices
from typing import Any, List, Union


class IDMixin(BaseModel):
    id: int


class TimestampMixin(BaseModel):
    created_at: datetime.datetime | None = None


class PublishMixin(BaseModel):
    published: bool = False


class ArticleBase(BaseModel):
    title: str
    title_foreign: str | None = None
    
    # Принимаем либо список строк, либо список объектов, либо просто строку
    authors: Union[List[Any], str, None] = None
    pages: str | None = None
    doi: str | None = None

    # Позволяем принимать и 'annotation', и 'abstract' (для совместимости)
    annotation: str | None = Field(None, validation_alias=AliasChoices('annotation', 'abstract'))
    annotation_foreign: str | None = None

    field_of_science: str | None = None

    # Принимаем список или строку
    keywords: Union[List[str], str, None] = None
    keywords_foreign: Union[List[str], str, None] = None

    pdf: str | None = None
    
    # Позволяем принимать и 'data', и 'publication_date'
    data: datetime.date | None = Field(None, validation_alias=AliasChoices('data', 'publication_date'))
    
    issue_id: int | None = None
    user_id: Any | None = None

    @field_validator('authors', 'keywords', 'keywords_foreign', mode='before')
    @classmethod
    def convert_list_to_string(cls, v: Any) -> str:
        if isinstance(v, list):
            if not v:
                return ""
            # Если это список авторов с объектами {"name": "..."}
            if isinstance(v[0], dict) and "name" in v[0]:
                return ", ".join([str(item["name"]) for item in v])
            # Обычный список строк
            return ", ".join([str(item) for item in v])
        return str(v) if v is not None else ""


class ArticleCreate(ArticleBase):
    model_config = ConfigDict(populate_by_name=True)


class ArticleUpdate(BaseModel):
    title: str | None = None
    title_foreign: str | None = None
    authors: Union[List[Any], str, None] = None
    pages: str | None = None
    doi: str | None = None
    annotation: str | None = Field(None, validation_alias=AliasChoices('annotation', 'abstract'))
    annotation_foreign: str | None = None
    field_of_science: str | None = None
    keywords: Union[List[str], str, None] = None
    keywords_foreign: Union[List[str], str, None] = None
    pdf: str | None = None
    published: bool | None = None
    data: datetime.date | None = Field(None, validation_alias=AliasChoices('data', 'publication_date'))


class ArticlePublic(
    IDMixin,
    ArticleBase,
    PublishMixin,
    TimestampMixin,
):
    model_config = ConfigDict(from_attributes=True)
    slug: str
    
    # В публичном ответе авторы и кейворды будут строками (как в БД)
    authors: str | None = None
    keywords: str | None = None

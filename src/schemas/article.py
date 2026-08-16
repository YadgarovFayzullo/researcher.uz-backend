"""Схемы статей и standalone-публикаций.

Одна таблица `articles` обслуживает статьи выпусков, монографии/диссертации
(`publication_type`) и доклады конференций (`conference_paper` + `section_id`),
поэтому полей много и почти все опциональны.
"""
import datetime
from typing import Any, List, Union, cast

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

# Порядок алиасов важен: при чтении из ORM (`from_attributes`) Pydantic идёт
# слева направо, а атрибут `metadata` у модели SQLAlchemy занят объектом
# MetaData — значит имя колонки `meta` должно проверяться первым.
_META_IN = AliasChoices("meta", "metadata")

_LIST_FIELDS = ("authors", "keywords", "keywords_foreign")


def _list_to_string(v: Any) -> str | None:
    """Списки схлопываем в строку — в БД это text-колонки.

    None остаётся None: иначе явно присланный null в PATCH затирал бы колонку
    пустой строкой вместо очистки.
    """
    if v is None:
        return None
    if isinstance(v, list):
        items = cast("list[Any]", v)
        if not items:
            return ""
        # Список авторов объектами {"name": "..."}
        if isinstance(items[0], dict) and "name" in items[0]:
            return ", ".join([str(item["name"]) for item in items])
        return ", ".join([str(item) for item in items])
    return str(v)


class IDMixin(BaseModel):
    id: int


class TimestampMixin(BaseModel):
    created_at: datetime.datetime | None = None


class PublishMixin(BaseModel):
    published: bool = False


class _ArticleCommon(BaseModel):
    """Поля с одинаковыми типами во входных (Create) и выходных (Public) схемах.

    `title`, `authors`, `keywords` сюда не входят: на входе это гибкие
    union-типы, а в публичном ответе — строки из БД, и общее объявление
    нарушало бы инвариантность типов при переопределении.
    """

    title_foreign: str | None = None
    pages: str | None = None
    doi: str | None = None

    # Позволяем принимать и 'annotation', и 'abstract' (для совместимости)
    annotation: str | None = Field(
        None, validation_alias=AliasChoices("annotation", "abstract")
    )
    annotation_foreign: str | None = None

    field_of_science: str | None = None

    # Принимаем список или строку
    keywords_foreign: Union[List[str], str, None] = None

    pdf: str | None = None

    # Позволяем принимать и 'data', и 'publication_date'
    data: datetime.date | None = Field(
        None, validation_alias=AliasChoices("data", "publication_date")
    )

    issue_id: int | None = None
    user_id: Any | None = None

    # --- standalone-публикации и доклады конференций ---
    publication_type: str | None = None
    isbn: str | None = None
    publisher: str | None = None  # внешний издатель, free-text
    publication_year: int | None = None
    cover_image: str | None = None
    publisher_id: int | None = None
    section_id: int | None = None
    meta: dict[str, Any] | None = Field(
        None, validation_alias=_META_IN, serialization_alias="metadata"
    )

    # check_fields=False: authors/keywords объявляются только в наследниках,
    # там валидатор и сработает.
    _norm = field_validator(*_LIST_FIELDS, mode="before", check_fields=False)(
        _list_to_string
    )


class ArticleBase(_ArticleCommon):
    title: str

    # Принимаем либо список строк, либо список объектов, либо просто строку
    authors: Union[List[Any], str, None] = None
    keywords: Union[List[str], str, None] = None


class ArticleCreate(ArticleBase):
    model_config = ConfigDict(populate_by_name=True)
    # Клиент может предложить слуг: форма статьи кладёт его в метаданные Zenodo
    # ДО создания записи, и придуманный сервером слуг разошёлся бы с тем, что
    # уже уехало во внешний DOI. Уникальность всё равно обеспечивает сервер.
    slug: str | None = None
    published: bool | None = None
    # Владелец standalone-публикации; проверяется в can_write_article.
    admin_id: Any | None = None


class ArticleUpdate(BaseModel):
    """Все поля опциональны; применяется через `exclude_unset` — не присланное
    поле не трогается, присланный null очищает колонку."""

    model_config = ConfigDict(populate_by_name=True)

    title: str | None = None
    title_foreign: str | None = None
    authors: Union[List[Any], str, None] = None
    pages: str | None = None
    doi: str | None = None
    annotation: str | None = Field(
        None, validation_alias=AliasChoices("annotation", "abstract")
    )
    annotation_foreign: str | None = None
    field_of_science: str | None = None
    keywords: Union[List[str], str, None] = None
    keywords_foreign: Union[List[str], str, None] = None
    pdf: str | None = None
    published: bool | None = None
    data: datetime.date | None = Field(
        None, validation_alias=AliasChoices("data", "publication_date")
    )

    issue_id: int | None = None
    publication_type: str | None = None
    isbn: str | None = None
    publisher: str | None = None
    publication_year: int | None = None
    cover_image: str | None = None
    publisher_id: int | None = None
    section_id: int | None = None
    meta: dict[str, Any] | None = Field(
        None, validation_alias=_META_IN, serialization_alias="metadata"
    )

    _norm = field_validator(*_LIST_FIELDS, mode="before")(_list_to_string)


class ArticlePublic(
    IDMixin,
    _ArticleCommon,
    PublishMixin,
    TimestampMixin,
):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    slug: str | None = None

    # В БД title nullable, поэтому в ответе он не обязателен — иначе старые
    # строки без заголовка валили бы весь список ResponseValidationError'ом.
    title: str | None = None

    # В публичном ответе авторы и кейворды — строки (как в БД)
    authors: str | None = None
    keywords: str | None = None
    admin_id: Any | None = None

    # Денормализация, которую доклеивает список (журнал/издатель/счётчики).
    journal_name: str | None = None
    journal_slug: str | None = None
    publisher_name: str | None = None
    views: int | None = None
    downloads: int | None = None


class ArticleListResponse(BaseModel):
    """Срез списка + общее число подходящих строк — для серверной пагинации."""

    items: List[ArticlePublic]
    total: int

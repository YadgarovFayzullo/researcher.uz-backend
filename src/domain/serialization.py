"""Сериализация ORM-строк в словари.

Нужна там, где ответ собирается вручную (строка модели + вычисленные поля вроде
`article_count`/`views`), а не отдаётся Pydantic'у напрямую.

Отдельный модуль — потому что `Model.__mapper__.column_attrs` для статических
анализаторов нетипизирован: разложенный по месту dict-comprehension даёт
`dict[Unknown, Unknown]`. Здесь незнание типов заперто в одной функции с явной
сигнатурой, и вызывающий код получает нормальный `dict[str, Any]`.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterable, cast


@lru_cache(maxsize=None)
def model_columns(model: type[Any], *, exclude: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Имена колонок модели (кешируется — набор статичен на время процесса)."""
    attrs = cast(Iterable[Any], model.__mapper__.column_attrs)
    return tuple(str(c.key) for c in attrs if str(c.key) not in exclude)


def row_to_dict(
    obj: Any, model: type[Any], *, exclude: tuple[str, ...] = ()
) -> dict[str, Any]:
    """ORM-строка → {колонка: значение}."""
    return {name: getattr(obj, name) for name in model_columns(model, exclude=exclude)}


# Колонки, которые никогда не уходят наружу: 768-мерный вектор эмбеддинга и
# tsvector-поля полнотекстового индекса. Последние содержат текст статьи
# целиком — попав в ответ, они раздувают его в десятки раз без всякой пользы
# для клиента. Держим список здесь, чтобы каждый сборщик ответа не заводил свой.
HEAVY_ARTICLE_COLUMNS = (
    "embedding",
    "document_ru",
    "document_en",
    "document_uz",
    "search_vector",
)

"""Обращение к языковой модели — один вход, три провайдера.

Слой проверки не должен зависеть от того, чей API за ним стоит: сверка
метаданных с PDF — задача несложная, её тянет и бесплатная модель, а платная
нужна только если бесплатных лимитов не хватает. Поэтому провайдер выбирается
переменной окружения, а домен зовёт одну функцию `structured()`.

Поддержаны:

* **gemini** — Google AI Studio. Бесплатный тариф с суточными лимитами,
  ключ заводится на aistudio.google.com. Разумный выбор по умолчанию.
* **openrouter** — витрина чужих моделей, среди них есть бесплатные
  (суффикс `:free` в имени). API OpenAI-совместимое.
* **anthropic** — Claude. Платный, лучше остальных держит сравнение
  «та же статья иначе оформлена или всё-таки другая».

Структура ответа задаётся схемой, а не просьбой «верни JSON»: у всех троих
есть режим принудительного JSON, и разбирать прозу регулярками не приходится.
Схему пишем в форме Anthropic (JSON Schema), для Gemini она переводится —
там свой диалект, не понимающий `additionalProperties` и типов-объединений.

Ошибка провайдера пробрасывается наверх: вызывающий обязан отличать
«проверено, чисто» от «проверить не удалось».
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from src.core.config import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 300.0
MAX_TOKENS = 8000


class LLMNotConfigured(RuntimeError):
    """Ни один провайдер не настроен — слой модели выключен."""


def provider() -> str | None:
    """Кто отвечает на запросы. None — никто не настроен.

    Явный LLM_PROVIDER главнее автоопределения: он же позволяет держать в .env
    сразу несколько ключей и переключаться между ними одной строкой.
    """
    explicit = (settings.LLM_PROVIDER or "").strip().lower()
    if explicit:
        return explicit or None
    if settings.GEMINI_API_KEY:
        return "gemini"
    if settings.OPENROUTER_API_KEY:
        return "openrouter"
    if settings.ANTHROPIC_API_KEY:
        return "anthropic"
    return None


def active_model() -> str:
    """Имя модели для записи в отчёт — по нему потом видно, чем проверяли."""
    name = provider()
    if name == "gemini":
        return f"gemini:{settings.GEMINI_MODEL}"
    if name == "openrouter":
        return f"openrouter:{settings.OPENROUTER_MODEL}"
    if name == "anthropic":
        return f"anthropic:{settings.AI_REVIEW_MODEL}"
    return "none"


def configured() -> bool:
    name = provider()
    if name == "gemini":
        return bool(settings.GEMINI_API_KEY)
    if name == "openrouter":
        return bool(settings.OPENROUTER_API_KEY and settings.OPENROUTER_MODEL)
    if name == "anthropic":
        return bool(settings.ANTHROPIC_API_KEY)
    return False


# --------------------------------------------------------------------------
# Перевод схемы под диалект Gemini
# --------------------------------------------------------------------------


def _to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """JSON Schema → responseSchema Gemini.

    Отличий три, и каждое иначе ломает запрос с 400: `additionalProperties`
    там не существует, тип-объединение `["string", "null"]` выражается флагом
    `nullable`, а порядок ключей объекта задаётся отдельным `propertyOrdering`
    (без него модель вольна переставлять поля, что нам безразлично, но и
    вредить не будет).
    """
    node = {k: v for k, v in schema.items() if k != "additionalProperties"}

    kind = node.get("type")
    if isinstance(kind, list):
        non_null = [t for t in kind if t != "null"]
        node["type"] = non_null[0] if non_null else "string"
        if "null" in kind:
            node["nullable"] = True

    if "properties" in node:
        node["properties"] = {
            key: _to_gemini_schema(value) for key, value in node["properties"].items()
        }
        node["propertyOrdering"] = list(node["properties"].keys())
    if "items" in node:
        node["items"] = _to_gemini_schema(node["items"])
    return node


# --------------------------------------------------------------------------
# Провайдеры
# --------------------------------------------------------------------------


async def _gemini(system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.GEMINI_MODEL}:generateContent"
    )
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _to_gemini_schema(schema),
            "maxOutputTokens": MAX_TOKENS,
            # Сверка фактов — не творчество: температура по нулям делает
            # вердикт воспроизводимым, а разбор одного и того же файла
            # дважды — одинаковым.
            "temperature": 0,
        },
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.post(
            url, json=payload, headers={"x-goog-api-key": settings.GEMINI_API_KEY}
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Gemini {response.status_code}: {response.text[:300]}")
    data = response.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Gemini вернул ответ без текста: {str(data)[:300]}") from e
    return json.loads(text)


async def _openrouter(system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "model": settings.OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "verdict", "strict": True, "schema": schema},
        },
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                # OpenRouter просит их для учёта трафика приложения; на
                # бесплатных моделях без них лимиты строже.
                "HTTP-Referer": settings.ADMIN_BASE_URL,
                "X-Title": "researcher.uz issue review",
            },
        )
    if response.status_code >= 400:
        raise RuntimeError(f"OpenRouter {response.status_code}: {response.text[:300]}")
    data = response.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"OpenRouter вернул ответ без текста: {str(data)[:300]}") from e
    return json.loads(text)


async def _anthropic(
    system: str, user: str, schema: dict[str, Any], model: str | None, thinking: bool
) -> dict[str, Any]:
    from src.infrastructure.external import claude

    return await claude.structured(
        system=system, user=user, schema=schema, model=model, thinking=thinking
    )


async def structured(
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    anthropic_model: str | None = None,
    thinking: bool = True,
) -> dict[str, Any]:
    """Один запрос к модели с гарантированной структурой ответа.

    `anthropic_model` и `thinking` касаются только Claude: разбору шапки PDF
    хватает дешёвой модели, сверке выпуска — нет. У Gemini и OpenRouter модель
    задаётся их переменными окружения.
    """
    name = provider()
    if not configured():
        raise LLMNotConfigured(f"провайдер {name or 'не выбран'} не настроен")
    if name == "gemini":
        return await _gemini(system, user, schema)
    if name == "openrouter":
        return await _openrouter(system, user, schema)
    if name == "anthropic":
        return await _anthropic(system, user, schema, anthropic_model, thinking)
    raise LLMNotConfigured(f"неизвестный LLM_PROVIDER: {name}")

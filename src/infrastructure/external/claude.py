"""Клиент Claude API — единственное место, где мы ходим в LLM.

Транспорт отделён от промпта сознательно: промпт живёт в домене
(`src/domain/ai_review.py`), потому что он часть правил модерации и меняется
вместе с ними, а здесь — только вызов и разбор ответа.

Ответ берём структурированным (`output_config.format` = json_schema): модель
обязана вернуть JSON по нашей схеме, и разбор не превращается в вылавливание
JSON из прозы регулярками. Ошибку API наверх пробрасываем — вызывающая сторона
обязана отличать «проверил, всё чисто» от «проверить не удалось»: первое
публикует статьи, второе не должно публиковать ничего.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from src.core.config import settings

logger = logging.getLogger(__name__)

# Ответ короткий (вердикт + список замечаний), но модель ещё и думает перед
# ним — на потолке в пару тысяч токенов ответ обрывался бы на середине JSON.
MAX_TOKENS = 16000
# Разбор трёх страниц PDF с включённым thinking занимает десятки секунд.
TIMEOUT_SECONDS = 300.0


class ClaudeNotConfigured(RuntimeError):
    """Ключа нет — вся ИИ-проверка выключена."""


@lru_cache(maxsize=1)
def _client():
    if not settings.ANTHROPIC_API_KEY:
        raise ClaudeNotConfigured("ANTHROPIC_API_KEY не задан")
    try:
        from anthropic import AsyncAnthropic
    except ImportError as e:  # pragma: no cover — пакет в requirements.txt
        raise ClaudeNotConfigured("пакет anthropic не установлен") from e

    return AsyncAnthropic(
        api_key=settings.ANTHROPIC_API_KEY,
        timeout=TIMEOUT_SECONDS,
        # Ретраи SDK (429 и 5xx) нам подходят: проверка не в горячем пути
        # запроса, лишняя минута ожидания никого не задерживает.
        max_retries=3,
    )


async def structured(
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    model: str | None = None,
    thinking: bool = True,
) -> dict[str, Any]:
    """Один запрос к модели с гарантированной структурой ответа.

    `thinking=False` не просит adaptive thinking явно. Нужно для Haiku 4.5
    (извлечение метаданных): adaptive он не принимает и отвечает 400. Модели,
    у которых thinking включён по умолчанию (Opus 5), подумают и так.
    """
    client = _client()
    extra: dict[str, Any] = {"thinking": {"type": "adaptive"}} if thinking else {}
    response = await client.messages.create(
        model=model or settings.AI_REVIEW_MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
        **extra,
    )
    if response.stop_reason == "refusal":
        # Отказ классификатора — не «статья плохая». Пробрасываем как сбой:
        # публиковать по отказу нельзя, гасить выпуск тоже не за что.
        raise RuntimeError(f"модель отклонила запрос: {response.stop_details}")

    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        raise RuntimeError("модель вернула пустой ответ")
    try:
        return json.loads(text)
    except ValueError as e:
        raise RuntimeError(f"ответ модели не разобрался как JSON: {text[:200]}") from e

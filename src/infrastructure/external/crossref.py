"""Транспорт депозита: отправка батча в Crossref и разбор его вердикта.

Депозит асинхронный и это его главная особенность. Сервлет отвечает «принято»
почти всегда — даже когда XML он потом отвергнет; настоящий результат забирают
отдельным запросом по `doi_batch_id`, иногда через минуты. Поэтому здесь два
вызова, а не один, и «отправлено» никогда не считается «зарегистрировано».

Песочница (`CROSSREF_ENV=test`) — тот же API на другом хосте и с отдельной
учёткой. DOI там не регистрируются: это единственное место, где можно
безнаказанно проверить схему, потому что боевой депозит необратим — DOI
обновляют, но не отзывают.
"""
from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree as ET

import httpx

from src.core.config import settings

DEPOSIT_TIMEOUT = 60.0
RESULT_TIMEOUT = 30.0

# Статусы батча у Crossref: пока не completed — результата ещё нет.
PENDING_BATCH_STATUSES = {"in_process", "queued", "submitted"}


class CrossrefTransportError(Exception):
    """Не удалось поговорить с Crossref (сеть, учётка, 5xx)."""


@dataclass(frozen=True)
class Credentials:
    """Учётка мембера, под которой уходит батч.

    Не глобальная: префикс принадлежит конкретному членству, и под чужой
    префикс наш логин задепонировать не может — батч вернётся ошибкой прав.
    Какому журналу какая учётка — решает домен (`resolve_credentials`).
    """

    login: str
    password: str
    account: str = "default"


@dataclass
class BatchResult:
    """Вердикт по батчу."""

    status: str          # completed | in_process | queued | unknown_submission
    success: int = 0
    failure: int = 0
    messages: list[str] = None  # type: ignore[assignment]
    raw: str = ""

    def __post_init__(self) -> None:
        if self.messages is None:
            self.messages = []

    @property
    def is_pending(self) -> bool:
        return self.status in PENDING_BATCH_STATUSES

    @property
    def is_success(self) -> bool:
        return self.status == "completed" and self.failure == 0 and self.success > 0


async def submit_batch(xml: bytes, *, batch_id: str, creds: Credentials) -> str:
    """Отправить батч. Возвращает текст ответа сервлета.

    Сервлет отвечает HTML-страницей: 200 + «SUCCESS» = батч принят в очередь,
    но НЕ разобран. Ошибку учётки он отдаёт кодом 401 либо тем же 200 с текстом
    про логин — поэтому проверяем и код, и тело.
    """
    url = f"{settings.CROSSREF_BASE_URL}/servlet/deposit"
    files = {"fname": (f"{batch_id}.xml", xml, "text/xml")}
    data = {
        "operation": "doMDUpload",
        "login_id": creds.login,
        "login_passwd": creds.password,
    }
    try:
        async with httpx.AsyncClient(timeout=DEPOSIT_TIMEOUT) as client:
            res = await client.post(url, data=data, files=files)
    except httpx.HTTPError as exc:
        raise CrossrefTransportError(f"Сеть: {exc}") from exc

    body = res.text or ""
    if res.status_code >= 400:
        raise CrossrefTransportError(f"HTTP {res.status_code}: {body[:500]}")
    lowered = body.lower()
    if "login" in lowered and "not found" in lowered:
        raise CrossrefTransportError(f"Crossref отверг учётку: {body[:500]}")
    return body


async def fetch_result(batch_id: str, *, creds: Credentials) -> BatchResult:
    """Забрать вердикт по батчу.

    `unknown_submission` — нормальный промежуточный ответ сразу после отправки:
    батч ещё не доехал до очереди. Считать его провалом нельзя.
    """
    url = f"{settings.CROSSREF_BASE_URL}/servlet/submissionDownload"
    params = {
        "usr": creds.login,
        "pwd": creds.password,
        "doi_batch_id": batch_id,
        "type": "result",
    }
    try:
        async with httpx.AsyncClient(timeout=RESULT_TIMEOUT) as client:
            res = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise CrossrefTransportError(f"Сеть: {exc}") from exc

    if res.status_code >= 400:
        raise CrossrefTransportError(f"HTTP {res.status_code}: {res.text[:500]}")
    return parse_result(res.text)


def parse_result(xml: str) -> BatchResult:
    """Разбор `doi_batch_diagnostic`.

    Отдельная от сети функция: разбор вердикта — то, что стоит проверять
    тестом на сохранённых ответах, не ходя в Crossref.
    """
    text = (xml or "").strip()
    if not text:
        return BatchResult(status="unknown_submission", raw=xml or "")

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # Сервлет иногда отвечает HTML-страницей вместо XML (батч ещё не
        # разобран, либо ошибка на его стороне).
        lowered = text.lower()
        status = "unknown_submission" if "unknown" in lowered else "in_process"
        return BatchResult(status=status, raw=text)

    status = (root.get("status") or "unknown_submission").strip()
    messages: list[str] = []
    counts: dict[str, int] = {}

    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in ("success_count", "failure_count", "warning_count"):
            try:
                counts[tag] = int((element.text or "0").strip())
            except ValueError:
                counts[tag] = 0
        elif tag == "record_diagnostic":
            record_status = (element.get("status") or "").strip()
            msg = " ".join(
                part.strip() for part in element.itertext() if part and part.strip()
            )
            messages.append(f"{record_status}: {msg}".strip(": ").strip())

    success = counts.get("success_count", 0)
    failure = counts.get("failure_count", 0)

    # Батч без batch_data (разбор ещё идёт) — считаем по записям, иначе
    # успешный батч выглядел бы «0 успехов» и никогда не закрывался.
    if not counts and messages:
        failure = sum(1 for m in messages if m.lower().startswith("failure"))
        success = len(messages) - failure

    return BatchResult(
        status=status,
        success=success,
        failure=failure,
        messages=messages[:20],
        raw=text[:20000],
    )

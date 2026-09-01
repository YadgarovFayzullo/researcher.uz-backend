"""Загрузка по адресу, который назвал пользователь.

Импорт со старого сайта журнала ходит по URL, введённому клиентом, — это
классический SSRF: без проверок таким запросом можно попасть в метаданные
облака (169.254.169.254), в соседний контейнер или в localhost самого API.

Правила здесь:
* только http/https — никаких file://, gopher://, ftp://;
* хост резолвится, и все его адреса проверяются на приватность (loopback,
  link-local, частные сети, CGNAT, зарезервированное);
* редиректы обрабатываем сами и проверяем КАЖДЫЙ переход: сайт может ответить
  302 на `http://127.0.0.1`, и проверка одного лишь исходного адреса ничего не
  стоила бы;
* тело ограничено по размеру и читается потоком — иначе «PDF» на 10 ГБ съест
  память контейнера;
* вежливость к чужому серверу: общий троттлинг на хост и внятный User-Agent с
  контактом, чтобы админ старого сайта видел, кто к нему ходит.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

USER_AGENT = "researcher.uz importer/1.0 (+https://researcher.uz; import@researcher.uz)"

# Пауза между обращениями к одному хосту. OJS-сайты небольших журналов живут на
# слабом хостинге — пачка параллельных запросов кладёт их и нас заодно банят
# (inlibrary.uz закрыл нам доступ на сутки ровно за это).
#
# По умолчанию секунда, но для разового прогона по согласованному архиву паузу
# можно сжать через IMPORT_MIN_INTERVAL: 429 и 5xx всё равно отрабатываются
# повтором с растущей задержкой, так что сервер сам себя защитит.
MIN_INTERVAL_SECONDS = float(os.environ.get("IMPORT_MIN_INTERVAL", "1.0"))
DEFAULT_TIMEOUT = 30.0
MAX_REDIRECTS = 5

_last_request_at: dict[str, float] = {}
_host_locks: dict[str, asyncio.Lock] = {}


class FetchError(Exception):
    """Ошибка загрузки, которую показываем клиенту как есть."""


class BlockedAddress(FetchError):
    """Адрес запрещён политикой (не публичный интернет)."""


@dataclass
class FetchResult:
    url: str          # адрес после всех редиректов
    status: int
    content: bytes
    content_type: str


def _is_public_ip(raw: str) -> bool:
    ip = ipaddress.ip_address(raw)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        # 100.64.0.0/10 — CGNAT: формально «не private», но в интернете его нет.
        or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"))
    )


def assert_public_url(url: str) -> None:
    """Пропустить только публичный http(s)-адрес. Иначе — BlockedAddress."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BlockedAddress(f"Разрешены только http и https, а не {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise BlockedAddress("В адресе нет хоста")

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as e:
        raise FetchError(f"Не удалось определить адрес {host}: {e}") from e

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise FetchError(f"Хост {host} не разрешается в адрес")
    for address in addresses:
        if not _is_public_ip(address):
            raise BlockedAddress(
                f"Адрес {host} ({address}) не публичный — импорт ходит только в интернет"
            )


async def _throttle(host: str) -> None:
    lock = _host_locks.setdefault(host, asyncio.Lock())
    async with lock:
        previous = _last_request_at.get(host)
        now = time.monotonic()
        if previous is not None:
            wait = MIN_INTERVAL_SECONDS - (now - previous)
            if wait > 0:
                await asyncio.sleep(wait)
        _last_request_at[host] = time.monotonic()


async def fetch(
    url: str,
    *,
    max_bytes: int,
    timeout: float = DEFAULT_TIMEOUT,
    accept: str | None = None,
    retries: int = 2,
) -> FetchResult:
    """Скачать документ, проверяя каждый адрес в цепочке редиректов."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        assert_public_url(current)
        host = urlparse(current).hostname or ""
        await _throttle(host)

        headers = {"User-Agent": USER_AGENT}
        if accept:
            headers["Accept"] = accept

        response = await _request_with_retry(current, headers, timeout, retries)
        # Клиент живёт ровно столько, сколько читается тело ответа. Забыть его
        # закрыть нельзя: на каждый запрос создаётся новый, и за тысячу статей
        # накапливается тысяча открытых пулов — импорт вставал наглухо, хотя
        # чужой сайт отвечал за доли секунды.
        client: httpx.AsyncClient | None = getattr(response, "_import_client", None)

        async def _release() -> None:
            await response.aclose()
            if client is not None:
                await client.aclose()

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            await _release()
            if not location:
                raise FetchError(f"Редирект без адреса: {current}")
            current = str(httpx.URL(current).join(location))
            continue

        if response.status_code >= 400:
            status = response.status_code
            await _release()
            raise FetchError(f"{current} → HTTP {status}")

        # Читаем потоком: без этого «файл» произвольного размера уедет в память
        # целиком ещё до того, как мы посмотрим на Content-Length.
        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise FetchError(
                        f"Файл больше допустимых {max_bytes // (1024 * 1024)} МБ"
                    )
        finally:
            await _release()

        return FetchResult(
            url=current,
            status=response.status_code,
            content=bytes(body),
            content_type=(response.headers.get("content-type") or "").split(";")[0].strip(),
        )

    raise FetchError(f"Слишком много редиректов: {url}")


async def _request_with_retry(
    url: str, headers: dict[str, str], timeout: float, retries: int
) -> httpx.Response:
    """GET с повтором на 429 и 5xx — чужой сервер имеет право приболеть."""
    delay = 2.0
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        try:
            request = client.build_request("GET", url, headers=headers)
            response = await client.send(request, stream=True)
            if response.status_code == 429 or response.status_code >= 500:
                await response.aclose()
                await client.aclose()
                last_error = FetchError(f"{url} → HTTP {response.status_code}")
                if attempt < retries:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise last_error
            # Клиент закроется вместе с ответом: держим его на время чтения тела.
            response._import_client = client  # type: ignore[attr-defined]
            return response
        except httpx.HTTPError as e:
            await client.aclose()
            last_error = FetchError(f"Не удалось загрузить {url}: {e}")
            if attempt < retries:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise last_error from e
    raise last_error or FetchError(f"Не удалось загрузить {url}")

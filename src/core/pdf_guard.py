"""Защита раздачи PDF от массовой выкачки.

Полные тексты — главная ценность платформы, и забрать их целиком проще всего
обходом `/pdf/<slug>.pdf`: адреса лежат в sitemap и в `citation_pdf_url`.
`robots.txt` закрывает эти пути от роботов и открывает только поисковикам, но
это просьба — скрейпер её игнорирует. Здесь стоит сам лимит.

Три вещи, без которых лимит калечит нормальную работу:

1. **Считаем файлы, а не запросы.** Просмотрщик (pdf.js) тянет один PDF
   десятком Range-запросов, и посчитай мы запросы — читатель улетал бы в бан
   на первой же статье. Повторное обращение к тому же файлу внутри окна
   бесплатно.
2. **Поисковики не трогаем.** Google (а значит и Scholar), Яндекс и Bing
   должны забирать полные тексты — на этом держится индексация. Опознаём их
   обратным DNS с подтверждением прямым: User-Agent подделывается строкой, и
   пускать по нему — значит не иметь лимита вовсе.
3. **IP читателя, а не прокси.** `/pdf/...` на researcher.uz обслуживает
   маршрут Next на Vercel, который ходит сюда сам; без проброса адреса все
   читатели слились бы в несколько адресов Vercel и забанили друг друга.
   Прокси присылает `X-Reader-IP` вместе с общим секретом, и только с ним мы
   этому заголовку верим.

Состояние держим в памяти процесса: бэкенд — один контейнер, а переживать его
перезапуск счётчику незачем.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time

from fastapi import Request

from src.core.config import settings

# UA, при котором вообще имеет смысл платить за обратный DNS. Сам по себе он
# ничего не доказывает — это лишь фильтр, чтобы не резолвить каждого читателя.
_SEARCH_UA = re.compile(r"googlebot|google-inspectiontool|yandex|bingbot|bingpreview", re.I)

# Домены, которым принадлежат настоящие краулеры поисковиков.
_SEARCH_DOMAINS = (
    ".googlebot.com",
    ".google.com",
    ".yandex.ru",
    ".yandex.net",
    ".yandex.com",
    ".search.msn.com",
)

# Вердикты обратного DNS: резолв дорогой, а адреса краулеров живут долго.
_BOT_TTL_SECONDS = 24 * 3600
_bot_cache: dict[str, tuple[bool, float]] = {}

# ip → {"files": {ключ файла: время}, "banned_until": время}
_visitors: dict[str, dict] = {}

# Потолок на размер словаря: защита от распылённого обхода с тысяч адресов,
# который иначе съел бы память процесса.
_MAX_VISITORS = 50_000


def reader_ip(request: Request) -> str | None:
    """Адрес читателя: из заголовка прокси, если он доказал, что он наш."""
    secret = settings.PDF_PROXY_SECRET
    if secret and request.headers.get("x-proxy-secret") == secret:
        forwarded = (request.headers.get("x-reader-ip") or "").strip()
        if forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
    return request.client.host if request.client else None


def _reverse_dns_ok(ip: str) -> bool:
    """Обратный DNS с подтверждением прямым — иначе PTR можно подделать."""
    try:
        host = socket.gethostbyaddr(ip)[0].lower()
    except (OSError, socket.herror, socket.gaierror):
        return False
    if not host.endswith(_SEARCH_DOMAINS):
        return False
    try:
        _, _, addresses = socket.gethostbyname_ex(host)
    except (OSError, socket.gaierror):
        return False
    return ip in addresses


async def is_search_engine(ip: str, user_agent: str | None) -> bool:
    """Настоящий краулер Google / Яндекса / Bing?"""
    if not user_agent or not _SEARCH_UA.search(user_agent):
        return False
    now = time.time()
    cached = _bot_cache.get(ip)
    if cached and cached[1] > now:
        return cached[0]
    verdict = await asyncio.to_thread(_reverse_dns_ok, ip)
    _bot_cache[ip] = (verdict, now + _BOT_TTL_SECONDS)
    return verdict


def _prune(now: float, window: float) -> None:
    for ip in list(_visitors):
        state = _visitors[ip]
        if state.get("banned_until", 0) > now:
            continue
        files = {k: t for k, t in state["files"].items() if now - t < window}
        if files:
            state["files"] = files
            state.pop("banned_until", None)
        else:
            _visitors.pop(ip, None)


async def check_download(request: Request, file_key: str) -> int | None:
    """Учесть выдачу файла. Вернёт секунды бана, если этому IP уже нельзя.

    `file_key` — сам файл (слаг статьи, id выпуска), а не URL: один файл,
    сколько бы Range-запросов его ни собирали, стоит одну единицу лимита.
    """
    limit = settings.PDF_RATE_LIMIT
    if limit <= 0:  # 0 — выключить лимит целиком, без выкатки кода
        return None

    ip = reader_ip(request)
    if not ip:
        return None
    if await is_search_engine(ip, request.headers.get("user-agent")):
        return None

    now = time.time()
    window = float(settings.PDF_RATE_WINDOW_SECONDS)
    _prune(now, window)

    state = _visitors.get(ip)
    if state is None:
        if len(_visitors) >= _MAX_VISITORS:
            # Словарь переполнен — считать перестаём, но и не баним: лучше
            # пропустить обход, чем закрыть сайт живым читателям.
            return None
        state = {"files": {}}
        _visitors[ip] = state

    banned_until = state.get("banned_until", 0)
    if banned_until > now:
        return int(banned_until - now) + 1

    if file_key in state["files"]:
        # Тот же файл в пределах окна: Range-запросы просмотрщика, перезагрузка
        # страницы, повторное открытие — всё это одна выдача.
        state["files"][file_key] = now
        return None

    state["files"][file_key] = now
    if len(state["files"]) > limit:
        ban = settings.PDF_BAN_SECONDS
        state["banned_until"] = now + ban
        return ban
    return None


def reset() -> None:
    """Сбросить состояние — для тестов."""
    _visitors.clear()
    _bot_cache.clear()

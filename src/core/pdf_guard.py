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

Состояние — в SQLite на диске контейнера, а НЕ в памяти процесса: uvicorn
поднимает несколько воркеров (`WEB_CONCURRENCY`, по умолчанию 2), запросы
раскидываются между ними случайно, и счётчик в памяти дал бы лимит, умноженный
на число воркеров, и дырявый бан — на соседнем воркере тот же гость снова
чистый. Файл общий для всех воркеров, переживать перезапуск контейнера ему не
нужно: бан живёт час.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import sqlite3
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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    ip       TEXT NOT NULL,
    file_key TEXT NOT NULL,
    at       REAL NOT NULL,
    PRIMARY KEY (ip, file_key)
);
CREATE INDEX IF NOT EXISTS downloads_at ON downloads (at);
CREATE TABLE IF NOT EXISTS bans (
    ip    TEXT PRIMARY KEY,
    until REAL NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.PDF_GUARD_DB, timeout=5)
    # WAL + busy_timeout: воркеры пишут в один файл, и без этого второй
    # получал бы "database is locked" вместо записи.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    return conn


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


def _record(ip: str, file_key: str, limit: int, window: float, ban: int) -> int | None:
    """Синхронная часть: учёт выдачи в общем для воркеров SQLite."""
    now = time.time()
    conn = _connect()
    # `with conn` фиксирует транзакцию, но НЕ закрывает соединение — закрываем
    # руками, иначе на каждой выдаче течёт файловый дескриптор.
    try:
        with conn:
            conn.execute("DELETE FROM downloads WHERE at < ?", (now - window,))
            conn.execute("DELETE FROM bans WHERE until <= ?", (now,))

            row = conn.execute("SELECT until FROM bans WHERE ip = ?", (ip,)).fetchone()
            if row:
                return int(row[0] - now) + 1

            # Тот же файл в пределах окна: Range-запросы просмотрщика,
            # перезагрузка страницы, повторное открытие — одна выдача.
            conn.execute(
                "INSERT INTO downloads (ip, file_key, at) VALUES (?, ?, ?) "
                "ON CONFLICT (ip, file_key) DO UPDATE SET at = excluded.at",
                (ip, file_key, now),
            )
            # Старые строки уже удалены выше, поэтому COUNT(*) — это и есть
            # число разных файлов, забранных за окно.
            distinct = conn.execute(
                "SELECT COUNT(*) FROM downloads WHERE ip = ?", (ip,)
            ).fetchone()[0]
            if distinct > limit:
                conn.execute(
                    "INSERT INTO bans (ip, until) VALUES (?, ?) "
                    "ON CONFLICT (ip) DO UPDATE SET until = excluded.until",
                    (ip, now + ban),
                )
                return ban
    finally:
        conn.close()
    return None


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

    try:
        return await asyncio.to_thread(
            _record,
            ip,
            file_key,
            limit,
            float(settings.PDF_RATE_WINDOW_SECONDS),
            settings.PDF_BAN_SECONDS,
        )
    except sqlite3.Error:
        # Счётчик сломался — раздачу это останавливать не должно: пропустить
        # обход хуже, чем закрыть сайт живым читателям, но ненамного, а вот
        # уронить 500 на каждом PDF — точно хуже обоих.
        return None


def reset() -> None:
    """Сбросить состояние — для тестов."""
    _bot_cache.clear()
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM downloads")
            conn.execute("DELETE FROM bans")
    finally:
        conn.close()

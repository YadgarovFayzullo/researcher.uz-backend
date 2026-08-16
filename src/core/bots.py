"""Отсев роботов при записи взаимодействий со статьями.

Зачем: просмотр засчитывается из браузера при открытии страницы статьи, а
поисковые краулеры исполняют JavaScript. К августу 2026 это дало 73% просмотров
от Googlebot (66.249.0.0/16) и ещё 8% от фермы headless-браузеров в Tencent
Cloud Singapore — статистика статей была завышена примерно в 5,5 раза.

Два фильтра, потому что у ботов два разных типа поведения:

* **User-Agent** ловит добросовестных краулеров (Googlebot, bingbot, YandexBot,
  SEO-сканеры, ИИ-скрейперы) — они честно представляются.
* **Сети** ловят фермы, которые подставляют UA обычного браузера; UA там
  бесполезен, остаётся адрес. Список задаётся через STATS_BLOCKED_NETWORKS,
  чтобы добавить сеть можно было переменной окружения, а не выкаткой кода.

Фильтр НЕ запрещает доступ: робот спокойно получает страницу и индексирует её.
Он лишь не попадает в счётчики просмотров.
"""
from __future__ import annotations

import ipaddress
import re

from fastapi import Request

from src.core.config import settings

# Токены в User-Agent, по которым робот опознаётся. Список намеренно широкий:
# ложно принять робота за человека хуже, чем наоборот — во втором случае мы
# теряем один просмотр, в первом портим статистику навсегда.
_BOT_UA = re.compile(
    r"""
    bot\b | \bbots?[/\s] | crawler | crawling | spider | slurp | scrapy
    | headlesschrome | phantomjs | puppeteer | playwright | selenium
    | python-requests | python-urllib | httpx | aiohttp | curl/ | wget
    | go-http-client | okhttp | java/ | node-fetch | axios | libwww | lwp-
    | facebookexternalhit | ia_archiver | feedfetcher | mediapartners
    | lighthouse | pagespeed | chrome-lighthouse | dataforseo | screaming\sfrog
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_bot_user_agent(user_agent: str | None) -> bool:
    """Пустой UA тоже считаем роботом: браузеры его всегда присылают."""
    if not user_agent or not user_agent.strip():
        return True
    return bool(_BOT_UA.search(user_agent))


def _blocked_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    raw = settings.STATS_BLOCKED_NETWORKS or ""
    nets = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            nets.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            # Кривая запись в переменной окружения не должна ронять приложение.
            continue
    return nets


def is_blocked_network(ip: str | None) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _blocked_networks())


def counts_as_human(request: Request) -> bool:
    """True — взаимодействие можно записывать в статистику."""
    if is_bot_user_agent(request.headers.get("user-agent")):
        return False
    ip = request.client.host if request.client else None
    return not is_blocked_network(ip)

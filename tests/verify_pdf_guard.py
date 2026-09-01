"""Лимит на массовую выкачку PDF (`src/core/pdf_guard.py`).

Что стережём:
  * читатель не улетает в бан из-за просмотрщика: один файл, сколько бы
    Range-запросов его ни собирали, стоит одну единицу лимита;
  * обход разных файлов упирается в порог и получает бан на срок из настроек;
  * бан держится и после того, как окно прошло бы для обычных запросов;
  * IP берётся из заголовка прокси только вместе с общим секретом — иначе
    любой, кто стучится в api.researcher.uz напрямую, менял бы себе адрес;
  * поисковики не лимитируются, но опознаются обратным DNS, а не User-Agent.

Сети и БД не нужны: обратный DNS подменяем.
"""
from __future__ import annotations

import asyncio

from src.core import pdf_guard
from src.core.config import settings

GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"
results: list[bool] = []


def check(name: str, got, want):
    ok = got == want
    results.append(ok)
    tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"{tag} {name}" + ("" if ok else f"  got={got!r} want={want!r}"))


class FakeClient:
    def __init__(self, host): self.host = host


class FakeRequest:
    def __init__(self, ip="203.0.113.7", headers=None):
        self.client = FakeClient(ip)
        self.headers = headers or {}


def download(req, key):
    return asyncio.run(pdf_guard.check_download(req, key))


# --- окно и порог берём маленькие, чтобы тест не зависел от прод-настроек
settings.PDF_RATE_LIMIT = 3
settings.PDF_BAN_SECONDS = 900
settings.PDF_PROXY_SECRET = "s3cret"

pdf_guard.reset()
req = FakeRequest()
check("один файл — не бан", [download(req, "article:1") for _ in range(10)], [None] * 10)

pdf_guard.reset()
req = FakeRequest()
got = [download(req, f"article:{i}") for i in range(1, 6)]
check("порог 3 файла: первые три проходят", got[:3], [None, None, None])
check("четвёртый файл ловит бан", got[3], 900)
check("после бана всё закрыто", got[4] is not None, True)

pdf_guard.reset()
mine = FakeRequest("198.51.100.1")
other = FakeRequest("198.51.100.2")
for i in range(1, 5):
    download(mine, f"article:{i}")
check("бан не задевает соседний IP", download(other, "article:9"), None)

pdf_guard.reset()
proxied = FakeRequest(
    "10.0.0.1", {"x-proxy-secret": "s3cret", "x-reader-ip": "192.0.2.55"}
)
check("с секретом верим адресу читателя", pdf_guard.reader_ip(proxied), "192.0.2.55")
forged = FakeRequest("10.0.0.1", {"x-proxy-secret": "wrong", "x-reader-ip": "192.0.2.55"})
check("без секрета заголовок игнорируется", pdf_guard.reader_ip(forged), "10.0.0.1")
bad_ip = FakeRequest("10.0.0.1", {"x-proxy-secret": "s3cret", "x-reader-ip": "не-адрес"})
check("мусор вместо адреса игнорируется", pdf_guard.reader_ip(bad_ip), "10.0.0.1")

# --- поисковики
pdf_guard.reset()
pdf_guard._reverse_dns_ok = lambda ip: ip == "66.249.66.1"  # type: ignore[assignment]
real_bot = FakeRequest("66.249.66.1", {"user-agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"})
fake_bot = FakeRequest("203.0.113.9", {"user-agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"})
check(
    "настоящий Googlebot без лимита",
    [download(real_bot, f"article:{i}") for i in range(1, 8)],
    [None] * 7,
)
got = [download(fake_bot, f"article:{i}") for i in range(1, 6)]
check("поддельный Googlebot ловит бан", got[3], 900)

pdf_guard.reset()
settings.PDF_RATE_LIMIT = 0
off = FakeRequest("203.0.113.77")
check(
    "PDF_RATE_LIMIT=0 выключает лимит",
    [download(off, f"article:{i}") for i in range(1, 10)],
    [None] * 9,
)

print(f"\n{sum(results)}/{len(results)} прошло")
raise SystemExit(0 if all(results) else 1)

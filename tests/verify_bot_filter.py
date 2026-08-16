"""Проверка отсева роботов в статистике (без сети и без БД).

Гоняет ровно то, что решает, засчитать просмотр или нет: опознание по
User-Agent и по сети из STATS_BLOCKED_NETWORKS.

    PYTHONPATH=. .venv/bin/python tests/verify_bot_filter.py
"""
from src.core.bots import counts_as_human, is_blocked_network, is_bot_user_agent

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results = []


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  [{PASS if ok else FAIL}] {name}: got={got} want={want}")


class FakeRequest:
    def __init__(self, ua=None, ip=None):
        self.headers = {"user-agent": ua} if ua is not None else {}
        self.client = type("C", (), {"host": ip})() if ip else None


BOTS = [
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    "Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)",
    "Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)",
    "Mozilla/5.0 (compatible; SemrushBot/7~bl)",
    "Mozilla/5.0 (Linux; Android 5.0) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/120",
    "python-requests/2.31.0",
    "curl/8.4.0",
    "Scrapy/2.11 (+https://scrapy.org)",
    "facebookexternalhit/1.1",
]

HUMANS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; SM-A536B) AppleWebKit/537.36 Chrome/125.0 Mobile Safari/537.36",
]

print("\n— опознание по User-Agent —")
for ua in BOTS:
    check(f"робот: {ua[:44]}", is_bot_user_agent(ua), True)
for ua in HUMANS:
    check(f"человек: {ua[:42]}", is_bot_user_agent(ua), False)
check("пустой UA — робот", is_bot_user_agent(""), True)
check("UA отсутствует — робот", is_bot_user_agent(None), True)

print("\n— опознание по сети —")
check("Tencent SG 43.172.5.5", is_blocked_network("43.172.5.5"), True)
check("Tencent SG 43.173.181.83", is_blocked_network("43.173.181.83"), True)
check("узбекский провайдер 84.54.90.1", is_blocked_network("84.54.90.1"), False)
check("мусор вместо адреса", is_blocked_network("не-адрес"), False)
check("адрес отсутствует", is_blocked_network(None), False)

print("\n— решение целиком —")
check("человек с обычного адреса", counts_as_human(FakeRequest(HUMANS[0], "84.54.90.1")), True)
check("Googlebot", counts_as_human(FakeRequest(BOTS[0], "66.249.66.1")), False)
check(
    "ферма: UA браузера, но адрес Tencent",
    counts_as_human(FakeRequest(HUMANS[0], "43.173.181.83")),
    False,
)

print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)

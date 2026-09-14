"""Покрытие каталога в Google Scholar: сколько наших статей он реально знает.

Зачем. Scholar не отдаёт ни API, ни панели вебмастера, а число на первой
странице выдачи — оценка, и она врёт: по `site:researcher.uz` он показывает
«примерно 699», а на последней странице остаётся 553. Поэтому считаем не
оценку, а фактические результаты — проходим пагинацию до конца.

Ходим не в сам Scholar (его robots.txt запрещает роботов, и там капча), а
через SerpApi, которая делает это легально. Ключ — в переменной окружения
SERPAPI_KEY, в репозитории ему не место.

Бесплатный тариф SerpApi — 100 запросов в месяц, одна страница выдачи это один
запрос. Поэтому: результаты кешируются на диск, и повторный прогон ничего не
тратит; --dry-run показывает, сколько запросов потребуется, не тратя их.

Запуск:
    SERPAPI_KEY=... .venv/bin/python scripts/scholar_coverage.py --dry-run
    SERPAPI_KEY=... .venv/bin/python scripts/scholar_coverage.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import httpx

SERPAPI = "https://serpapi.com/search"
SITE = "site:researcher.uz"
PAGE = 20  # Scholar отдаёт максимум 20 записей на страницу.

# Годы, в которых у нас есть статьи. Разбивку по журналам НЕ спрашиваем у
# Scholar оператором `source:` — в связке с `site:` он возвращает пустоту.
# Вместо этого забираем всё за год и раскладываем по журналам сами, из поля
# publication_info, которое Scholar печатает под каждым результатом.
YEARS = [2023, 2024, 2025, 2026]

CACHE = Path(__file__).resolve().parent.parent / ".scholar_cache.json"


def load_cache() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def fetch_page(key: str, query: str, year: int, start: int, cache: dict) -> dict:
    """Одна страница выдачи. Кеш — чтобы не платить за повтор."""
    ck = f"{query}|{year}|{start}"
    if ck in cache:
        return cache[ck]

    params = {
        "engine": "google_scholar",
        "q": query,
        "as_ylo": year,
        "as_yhi": year,
        "start": start,
        "num": PAGE,
        "api_key": key,
    }
    r = httpx.get(SERPAPI, params=params, timeout=90)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        # «hasn't returned any results» — легальный ответ «ничего не нашлось».
        if "any results" not in str(data["error"]):
            raise SystemExit(f"SerpApi: {data['error']}")
        data = {"organic_results": []}

    # Храним только нужное: ключ и служебное в кеш не кладём.
    page = {
        "results": [
            {
                "title": it.get("title"),
                "link": it.get("link"),
                # «Журнал, год - researcher.uz» — отсюда берём название журнала.
                "pub": ((it.get("publication_info") or {}).get("summary") or ""),
            }
            for it in data.get("organic_results", [])
        ],
        "estimate": (data.get("search_information") or {}).get("total_results"),
    }
    cache[ck] = page
    save_cache(cache)
    return page


def slug_of(link: str | None) -> str | None:
    """Слаг нашей статьи из ссылки Scholar (у него бывает и ссылка на PDF)."""
    if not link:
        return None
    m = re.search(r"researcher\.uz/(?:[a-z]{2}/)?article/([A-Za-z0-9._-]+)", link)
    if m:
        return m.group(1)
    m = re.search(r"researcher\.uz/pdf/([A-Za-z0-9._-]+?)\.pdf", link)
    return m.group(1) if m else None


# Scholar печатает название журнала как придётся: обрезает многоточием
# («Journal of Universal …»), меняет регистр, иногда даёт аббревиатуру. Без
# схлопывания один журнал распадается на десяток строк, поэтому сводим
# варианты к каноническому названию по началу строки.
KNOWN_JOURNALS = [
    ("journal of universal", "Journal of Universal Science Research"),
    ("j. univ. sci", "Journal of Universal Science Research"),
    ("inter education", "Inter Education & Global Study"),
    ("inter study", "Inter Study"),
]


def normalize_journal(name: str) -> str:
    low = name.strip().lower().lstrip("[").strip()
    for prefix, canonical in KNOWN_JOURNALS:
        if low.startswith(prefix):
            return canonical
    return "(прочее / не распознано)"


def journal_of(pub: str) -> str:
    """Название журнала из строки вида «S Eshkoraev - Journal of …, 2024 - researcher.uz»."""
    body = pub.split(" - ")
    if len(body) < 2:
        return "(не определён)"
    # Средняя часть — «Журнал, год»; год отрезаем.
    middle = body[1]
    return re.sub(r",\s*\d{4}\s*$", "", middle).strip() or "(не определён)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="только оценка расхода запросов")
    ap.add_argument(
        "--offline",
        action="store_true",
        help="считать только по кешу, в сеть не ходить (лимит SerpApi исчерпан)",
    )
    args = ap.parse_args()

    key = os.environ.get("SERPAPI_KEY", "").strip()
    if not key and not (args.dry_run or args.offline):
        print("Нет SERPAPI_KEY в окружении.", file=sys.stderr)
        return 2

    cache = load_cache()
    spent = 0
    records: list[dict] = []
    incomplete: set[int] = set()

    for year in YEARS:
        start_at = 0
        while True:
            ck = f"{SITE}|{year}|{start_at}"
            cached = ck in cache
            if args.dry_run and not cached:
                spent += 1
                break
            if args.offline and not cached:
                # Дальше этой страницы кеш не заполнен: год посчитан не целиком,
                # и честнее сказать об этом, чем выдать обрезанный итог за полный.
                incomplete.add(year)
                break
            page = fetch_page(key, SITE, year, start_at, cache)
            if not cached:
                spent += 1
            got = page["results"]
            for it in got:
                records.append({**it, "year": year})
            print(f"  {year}: +{len(got)} (всего {len(records)})", file=sys.stderr)
            if len(got) < PAGE:
                break
            start_at += PAGE

    if args.dry_run:
        print(f"Запросов потребуется минимум: {spent} (пагинация добавит ещё)")
        return 0

    # Сводка: журнал × год.
    table: dict[tuple[str, int], int] = {}
    slugs: set[str] = set()
    for r in records:
        j = normalize_journal(journal_of(r["pub"]))
        table[(j, r["year"])] = table.get((j, r["year"]), 0) + 1
        s_ = slug_of(r["link"])
        if s_:
            slugs.add(s_)

    journals = sorted({j for j, _ in table}, key=lambda j: -sum(
        v for (jj, _), v in table.items() if jj == j))

    head = f"{'журнал':<44}" + "".join(f"{y:>7}" for y in YEARS) + f"{'всего':>8}"
    print("\n" + head)
    print("-" * len(head))
    for j in journals:
        cells = [table.get((j, y), 0) for y in YEARS]
        print(f"{j[:44]:<44}" + "".join(f"{c:>7}" for c in cells) + f"{sum(cells):>8}")
    print("-" * len(head))
    totals = [sum(table.get((j, y), 0) for j in journals) for y in YEARS]
    print(f"{'ИТОГО':<44}" + "".join(f"{t:>7}" for t in totals) + f"{sum(totals):>8}")

    out = CACHE.parent / "scholar_slugs.txt"
    out.write_text("\n".join(sorted(slugs)), encoding="utf-8")
    if incomplete:
        print(f"\nНЕПОЛНЫЕ годы (кеш оборван, реальные числа больше): {sorted(incomplete)}")
    print(f"\nЗаписей получено: {len(records)}; слагов распознано: {len(slugs)} → {out}")
    print(f"Запросов к SerpApi за прогон: {spent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

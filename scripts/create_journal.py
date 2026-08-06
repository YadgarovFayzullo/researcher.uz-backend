"""Завести журнал через API (нужна роль owner).

Пароль скрипт спрашивает интерактивно и нигде не сохраняет — в аргументах его
передавать не надо, иначе он осядет в истории командной строки.

    # посмотреть, что будет отправлено, ничего не создавая
    python scripts/create_journal.py --name "Scientific Journal of BUT" --dry-run

    # создать на проде
    python scripts/create_journal.py \
        --api https://api.researcher.uz \
        --email you@example.com \
        --name "Scientific Journal of BUT" \
        --issn 1234-5678 \
        --site https://example.uz \
        --theme "Технические науки" \
        --description "…" \
        --vak

Слаг по умолчанию выводится из названия теми же правилами, что и на сайте
(включая узбекскую кириллицу). Проверить, что получится, можно с --dry-run.
"""
from __future__ import annotations

import argparse
import getpass
import json
import re
import urllib.error
import urllib.request

DEFAULT_API = "https://api.researcher.uz"

# Правила слага повторяют src/lib/publications.ts на фронте — именно они
# определяют адрес страницы. Держим на голой стандартной библиотеке, чтобы
# скрипт запускался где угодно без установки зависимостей бэкенда.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "қ": "q", "ў": "o", "ғ": "g", "ҳ": "h",
    " ": "-", "_": "-", "'": "", '"': "", "–": "-", "—": "-",
    "ʻ": "", "ʼ": "", "‘": "", "’": "", "`": "",
}
_UZ_MARKERS = re.compile("[қўғҳ]")
_UZ_OVERRIDES = {"ж": "j", "х": "x", "ё": "yo"}


def slugify_latin(text: str) -> str:
    lower = (text or "").lower()
    table = dict(_TRANSLIT)
    if _UZ_MARKERS.search(lower):
        table.update(_UZ_OVERRIDES)
    out = "".join(table.get(ch, ch) for ch in lower)
    out = re.sub(r"[^a-z0-9\-]", "-", out)
    return re.sub(r"-+", "-", out).strip("-")


def request_json(
    url: str, *, method: str = "GET", body: dict | None = None, token: str | None = None
) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"{method} {url} → {e.code}\n{detail}") from e
    except urllib.error.URLError as e:
        raise SystemExit(f"Не удалось подключиться к {url}: {e.reason}") from e


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api", default=DEFAULT_API, help=f"по умолчанию {DEFAULT_API}")
    p.add_argument("--email", help="учётка владельца (не нужна при --dry-run)")
    p.add_argument("--name", required=True)
    p.add_argument("--slug", help="по умолчанию выводится из названия")
    p.add_argument("--issn", help="электронный ISSN")
    p.add_argument("--printed-issn")
    p.add_argument("--site", help="ссылка на сайт журнала")
    p.add_argument("--scholar", help="ссылка на профиль Google Scholar")
    p.add_argument("--theme", help="категория/тематика")
    p.add_argument("--description")
    p.add_argument("--publisher", help="издающая организация")
    p.add_argument(
        "--type",
        default="journal",
        choices=["journal", "conference_series"],
        help="по умолчанию journal",
    )
    p.add_argument("--vak", action="store_true", help="входит в перечень ВАК")
    p.add_argument("--dry-run", action="store_true", help="только показать тело запроса")
    args = p.parse_args()

    slug = args.slug or slugify_latin(args.name)
    payload = {
        "name": args.name,
        "slug": slug,
        "type": args.type,
        # Пустые поля не отправляем: у колонок есть свои значения по умолчанию,
        # а null затирал бы их без нужды.
        **({"issn": args.issn} if args.issn else {}),
        **({"printed_issn": args.printed_issn} if args.printed_issn else {}),
        **({"site_link": args.site} if args.site else {}),
        **({"google_scholar": args.scholar} if args.scholar else {}),
        **({"theme": args.theme} if args.theme else {}),
        **({"description": args.description} if args.description else {}),
        **({"publisher": args.publisher} if args.publisher else {}),
        **({"vak": "1"} if args.vak else {}),
    }

    print(f"API:  {args.api}")
    print(f"Слаг: {slug}   →  {args.api.replace('api.', '')}/uz/journal/{slug}")
    print("Тело запроса:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if args.dry_run:
        print("\n--dry-run: ничего не отправлено.")
        return 0

    if not args.email:
        raise SystemExit("Без --dry-run нужен --email владельца.")

    password = getpass.getpass(f"Пароль для {args.email}: ")
    tokens = request_json(
        f"{args.api}/auth/login",
        method="POST",
        body={"email": args.email, "password": password},
    )
    token = tokens.get("access_token")
    if not token:
        raise SystemExit("Логин не вернул access_token.")

    me = request_json(f"{args.api}/auth/me", token=token)
    role = (me.get("profile") or {}).get("role")
    if role != "owner":
        raise SystemExit(f"Нужна роль owner, а у {args.email} роль: {role!r}")

    created = request_json(
        f"{args.api}/journals/", method="POST", body=payload, token=token
    )
    print("\nСоздан журнал:")
    print(f"  id:   {created.get('id')}")
    print(f"  slug: {created.get('slug')}")
    print(f"  имя:  {created.get('name')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

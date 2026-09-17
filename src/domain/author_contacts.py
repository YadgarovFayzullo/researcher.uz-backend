"""Почта авторов из текста статьи.

Зачем. Рассылка «ваша статья на researcher.uz» держится на адресах, а взять их
негде: в базе почты авторов не было, список на 1430 адресов собрали разово и со
статьями не связали. При этом адреса напечатаны в самих статьях — в узбекских
журналах в конце есть блок «Сведения об авторах»: ФИО, место работы, адрес,
e-mail, иногда ORCID. По замеру на 2653 статьях с извлечённым текстом почта есть
у 570 (21%), и 545 из них — именно в этом хвостовом блоке, а не в шапке.

Кому какой адрес принадлежит, решаем консервативно: по ORCID, затем по
похожести имени рядом с адресом (`may_be_same_person`, та же мягкая сверка, что
у «Это я», и так же только внутри одной статьи), а если авторов и адресов по
одному — связываем напрямую. В остальных случаях адрес не привязываем: чужая
почта в подписи хуже, чем её отсутствие.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.author_names import may_be_same_person
from src.infrastructure.persistence.models import ArticleAuthor, ArticleText

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# В PDF латинские буквы в домене регулярно оказываются кириллическими
# («gmail.сom»). Ищем с их допуском, а формат проверяем уже после замены.
LOOKALIKE_PAIRS = {"с": "c", "о": "o", "е": "e", "а": "a", "р": "p", "х": "x", "у": "y", "м": "m"}
LOOKALIKE = str.maketrans(LOOKALIKE_PAIRS)
_LOOK = "".join(LOOKALIKE_PAIRS)
SCAN_RE = re.compile(
    rf"[A-Za-z0-9._%+-]+@[A-Za-z0-9.\-{_LOOK}]+\.[A-Za-z{_LOOK}]{{2,}}"
)
ORCID_RE = re.compile(r"\b(\d{4}-\d{4}-\d{4}-\d{3}[\dX])\b")
# Имя рядом с адресом: 2–4 слова с большой буквы, латиница или кириллица.
NAME_RE = re.compile(
    r"[A-ZА-ЯЁЎҚҒҲ][A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ’'`.\-]+"
    r"(?:\s+[A-ZА-ЯЁЎҚҒҲ][A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ’'`.\-]*){1,3}"
)
# Сколько знаков перед адресом считаем его окружением: в блоке сведений об
# авторах между ФИО и почтой стоит место работы и адрес.
WINDOW = 400

# Ящики редакции и сервисов: они принадлежат журналу, а не автору, и письмо
# «ваша статья» такому адресату бессмысленно.
ROLE_LOCALS = frozenset({
    "info", "editor", "editors", "office", "support", "admin", "journal",
    "redaktor", "no-reply", "noreply", "contact", "mail", "email", "rektor",
    "press", "sekretar", "secretary", "publisher", "submissions",
})

@dataclass(frozen=True)
class Contact:
    email: str
    orcid: str | None
    names: tuple[str, ...]


def normalize_email(raw: str) -> str | None:
    email = raw.strip().strip(".,;:()[]<>").lower()
    local, _, domain = email.partition("@")
    email = f"{local}@{domain.translate(LOOKALIKE)}"
    if not EMAIL_RE.fullmatch(email) or local in ROLE_LOCALS:
        return None
    return email


def parse_contacts(text: str) -> list[Contact]:
    """Адреса из текста статьи вместе с ORCID и именами рядом с ними."""
    contacts: dict[str, Contact] = {}
    for match in SCAN_RE.finditer(text or ""):
        email = normalize_email(match.group(0))
        if email is None or email in contacts:
            continue
        before = text[max(0, match.start() - WINDOW):match.start()]
        # ORCID печатают и до адреса, и сразу после него.
        after = text[match.end():match.end() + 120]
        orcids = ORCID_RE.findall(before) + ORCID_RE.findall(after)
        names = tuple(NAME_RE.findall(before))[-4:]
        contacts[email] = Contact(email, orcids[-1] if orcids else None, names)
    return list(contacts.values())


def _match(rows: list[ArticleAuthor], contacts: list[Contact]) -> dict:
    """Кому какой адрес. Спорные случаи оставляем без адреса."""
    pairs: dict[str, str] = {}
    used: set[str] = set()

    by_orcid = {r.orcid: r for r in rows if r.orcid}
    for c in contacts:
        row = by_orcid.get(c.orcid) if c.orcid else None
        if row is not None and row.id not in pairs:
            pairs[row.id] = c.email
            used.add(c.email)

    for c in contacts:
        if c.email in used:
            continue
        hits = [
            r for r in rows
            if r.id not in pairs and any(may_be_same_person(r.author_name, n) for n in c.names)
        ]
        # Двух однофамильцев рядом с одним адресом не разбираем.
        if len(hits) == 1:
            pairs[hits[0].id] = c.email
            used.add(c.email)

    free_rows = [r for r in rows if r.id not in pairs]
    free_mails = [c.email for c in contacts if c.email not in used]
    if len(free_rows) == 1 and len(free_mails) == 1:
        pairs[free_rows[0].id] = free_mails[0]

    return pairs


async def attach_article_contacts(
    db: AsyncSession, article_id: int, text: str | None = None, *, overwrite: bool = False
) -> dict[str, int]:
    """Проставить почту подписям статьи. Возвращает счётчики для лога."""
    if text is None:
        text = (
            await db.execute(
                select(ArticleText.content).where(ArticleText.article_id == article_id)
            )
        ).scalar_one_or_none()
    if not text or "@" not in text:
        return {"contacts": 0, "matched": 0, "written": 0}

    contacts = parse_contacts(text)
    if not contacts:
        return {"contacts": 0, "matched": 0, "written": 0}

    rows = list(
        (
            await db.execute(
                select(ArticleAuthor).where(
                    ArticleAuthor.article_id == article_id,
                    ArticleAuthor.from_claim.is_(False),
                )
            )
        ).scalars()
    )
    pairs = _match(rows, contacts)

    written = 0
    for row in rows:
        email = pairs.get(row.id)
        if not email or (row.email and not overwrite):
            continue
        row.email = email
        written += 1
    return {"contacts": len(contacts), "matched": len(pairs), "written": written}

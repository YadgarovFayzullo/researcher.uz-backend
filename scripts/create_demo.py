"""Демо-стенд: журнал, доступ для клиента и немного контента.

Заводит на проде журнал с `metadata.demo = true` (см. `src/domain/demo.py` —
такой журнал не попадает в каталоги, ленты, поиск и sitemap), учётку с ролью
`admin`, привязанную к нему через `journal_admins`, один выпуск и пару статей
с PDF. Дальше клиенту дают логин и ссылку — он видит настоящую админку, не
задевая витрину.

Идемпотентно: повторный запуск ничего не дублирует, только досоздаёт
недостающее (журнал ищется по слагу, пользователь по email, статьи по слагу).
Скрипт ходит прямо в БД, поэтому запускать его надо там, где задан
DATABASE_URL — на проде это контейнер api:

    ssh ubuntu@<host> 'cd ~/app && sudo docker compose -f docker-compose.prod.yml \\
        exec -T -e DEMO_PASSWORD=... api python scripts/create_demo.py'

Пароль берётся из DEMO_PASSWORD (в аргументах он осел бы в истории команд).
`--dry-run` показывает, что будет создано, ничего не записывая.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from datetime import date

from sqlalchemy import select

from src.core.security import hash_password
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article,
    Identity_,
    Issue,
    Journal,
    JournalAdmin,
    Profile,
    User,
)

JOURNAL_SLUG = "scientific-journal-demo"
JOURNAL_NAME = "Scientific Journal (демонстрационный)"
DEMO_EMAIL = "demo@researcher.uz"
DEMO_FULL_NAME = "Демо-редактор"

# Статьи-образцы. Показывают заполненную карточку: авторы, аннотация, ключевые
# слова, страницы — то, что клиент будет заполнять сам.
DEMO_ARTICLES = [
    {
        "slug": "demo-article-machine-learning-in-materials-science",
        "title": "Применение машинного обучения в материаловедении",
        "title_foreign": "Machine learning applications in materials science",
        "authors": "Каримов А.А., Petrova E.V.",
        "annotation": (
            "В статье рассматриваются подходы машинного обучения к предсказанию "
            "свойств новых материалов. Это демонстрационная публикация: текст "
            "приведён для примера оформления карточки статьи."
        ),
        "keywords": "машинное обучение, материаловедение, предсказание свойств",
        "pages": "5-14",
        "field_of_science": "Технические науки",
    },
    {
        "slug": "demo-article-water-resources-management",
        "title": "Управление водными ресурсами в аридной зоне",
        "title_foreign": "Water resources management in arid regions",
        "authors": "Юсупова Н.Р.",
        "annotation": (
            "Обзор практик водопользования и оценка эффективности капельного "
            "орошения. Демонстрационная публикация для показа возможностей "
            "платформы."
        ),
        "keywords": "водные ресурсы, орошение, аридная зона",
        "pages": "15-23",
        "field_of_science": "Сельскохозяйственные науки",
    },
    {
        "slug": "demo-article-digital-humanities-corpus",
        "title": "Цифровые методы в корпусной лингвистике",
        "title_foreign": "Digital methods in corpus linguistics",
        "authors": "Абдуллаев Ж.Т., Ким С.",
        "annotation": (
            "Как корпусные методы меняют исследования языка. Демонстрационная "
            "публикация."
        ),
        "keywords": "корпусная лингвистика, цифровые методы, NLP",
        "pages": "24-31",
        "field_of_science": "Гуманитарные науки",
    },
]


def sample_pdf(title: str) -> bytes:
    """Минимальный одностраничный PDF-заглушка (собран вручную, без зависимостей).

    Текст латиницей: базовые шрифты PDF (Helvetica/WinAnsi) кириллицу не несут,
    а тащить ради демо-файла шрифт и генератор незачем — файл нужен, чтобы в
    просмотрщике и в счётчике скачиваний было что показать.
    """
    lines = [
        "Demo publication - researcher.uz",
        "",
        title[:70],
        "",
        "This is a sample PDF used in the demo journal.",
        "Upload your own file when adding a real article.",
    ]
    text_ops = "BT /F1 14 Tf 72 720 Td 18 TL\n" + "".join(
        f"({l.replace('(', '').replace(')', '')}) Tj T*\n" for l in lines
    ) + "ET"
    stream = text_ops.encode("latin-1", "replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n".encode()
        + b"%%EOF\n"
    )
    return bytes(out)


async def upload_pdf(slug: str, title: str) -> str | None:
    """Положить PDF-заглушку в R2 и вернуть публичный URL (или None без R2)."""
    from src.infrastructure.storage import StorageNotConfigured, public_url, storage

    key = f"pdfs/{slug}.pdf"
    try:
        storage.put(key, sample_pdf(title), "application/pdf")
    except StorageNotConfigured:
        print("  R2 не сконфигурирован — статьи будут без PDF")
        return None
    return public_url(key)


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--email", default=DEMO_EMAIL)
    p.add_argument("--slug", default=JOURNAL_SLUG)
    p.add_argument("--skip-pdf", action="store_true", help="не загружать PDF в R2")
    args = p.parse_args()

    password = os.environ.get("DEMO_PASSWORD")
    if not password and not args.dry_run:
        raise SystemExit("Задай пароль демо-учётки в переменной DEMO_PASSWORD.")

    if args.dry_run:
        print(f"Журнал: {JOURNAL_NAME}  /uz/journal/{args.slug}  (metadata.demo=true)")
        print(f"Учётка: {args.email}  role=admin, привязка journal_admins")
        print(f"Выпуск: {date.today().year}, том 1, № 1 + {len(DEMO_ARTICLES)} статьи")
        return 0

    async with AsyncSessionLocal() as db:
        # --- журнал --------------------------------------------------------
        journal = (
            await db.execute(select(Journal).where(Journal.slug == args.slug))
        ).scalars().first()
        if journal is None:
            journal = Journal(
                name=JOURNAL_NAME,
                slug=args.slug,
                type="journal",
                description=(
                    "Демонстрационный журнал платформы researcher.uz. Здесь можно "
                    "посмотреть, как выглядит журнал, выпуск и статья, и попробовать "
                    "редакторскую панель."
                ),
                theme="Мультидисциплинарный",
                publisher="researcher.uz",
                meta={"demo": True},
            )
            db.add(journal)
            await db.flush()
            print(f"+ журнал id={journal.id}")
        else:
            # Флаг проставляем и на уже существующем журнале: без него демо
            # утечёт в каталог и поиск.
            journal.meta = {**(journal.meta or {}), "demo": True}
            print(f"= журнал уже есть, id={journal.id}")

        # --- учётка клиента -------------------------------------------------
        user = (
            await db.execute(select(User).where(User.email == args.email))
        ).scalars().first()
        if user is None:
            user = User(
                id=uuid.uuid4(),
                email=args.email,
                password_hash=hash_password(password),
            )
            db.add(user)
            await db.flush()
            db.add(Profile(id=user.id, full_name=DEMO_FULL_NAME, role="admin"))
            db.add(
                Identity_(
                    user_id=user.id,
                    provider="email",
                    provider_id=str(user.id),
                    identity_data={"email": args.email},
                )
            )
            print(f"+ пользователь {args.email}")
        else:
            # Пароль перевыставляем: скрипт — единственный источник правды о том,
            # что выдано клиенту.
            user.password_hash = hash_password(password)
            profile = (
                await db.execute(select(Profile).where(Profile.id == user.id))
            ).scalars().first()
            if profile is None:
                db.add(Profile(id=user.id, full_name=DEMO_FULL_NAME, role="admin"))
            elif profile.role not in ("owner", "admin"):
                profile.role = "admin"
            print(f"= пользователь {args.email} уже есть — пароль обновлён")

        # --- привязка админа к журналу --------------------------------------
        link = (
            await db.execute(
                select(JournalAdmin).where(
                    JournalAdmin.journal_id == journal.id,
                    JournalAdmin.user_id == user.id,
                )
            )
        ).scalars().first()
        if link is None:
            db.add(JournalAdmin(journal_id=journal.id, user_id=user.id))
            print("+ привязка journal_admins")

        # --- выпуск ---------------------------------------------------------
        issue = (
            await db.execute(
                select(Issue).where(Issue.journal_id == journal.id).order_by(Issue.id)
            )
        ).scalars().first()
        if issue is None:
            issue = Issue(
                journal_id=journal.id,
                year=date.today().year,
                volume="1",
                issue="1",
                title=f"Том 1, № 1 ({date.today().year})",
            )
            db.add(issue)
            await db.flush()
            print(f"+ выпуск id={issue.id}")
        else:
            print(f"= выпуск уже есть, id={issue.id}")

        # --- статьи ----------------------------------------------------------
        for item in DEMO_ARTICLES:
            exists = (
                await db.execute(select(Article).where(Article.slug == item["slug"]))
            ).scalars().first()
            if exists is not None:
                print(f"= статья {item['slug']} уже есть")
                continue
            pdf_url = (
                None if args.skip_pdf else await upload_pdf(item["slug"], item["title"])
            )
            db.add(
                Article(
                    issue_id=issue.id,
                    publication_type="article",
                    published=True,
                    data=date.today(),
                    publication_year=date.today().year,
                    pdf=pdf_url,
                    **item,
                )
            )
            print(f"+ статья {item['slug']}" + ("" if pdf_url else " (без PDF)"))

        await db.commit()

    print(f"\nГотово. Журнал: https://researcher.uz/uz/journal/{args.slug}")
    print(f"Вход: {args.email} — админка: https://researcher.uz/ru/admin")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

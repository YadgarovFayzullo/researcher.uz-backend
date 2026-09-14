"""Загрузка папки PDF: метаданные из самих файлов и путь до статьи.

Стережём то, ради чего фича и делалась, — редактор кидает папку, а разбирает
её платформа:
  * regex вытаскивает страницы, том, номер и ISSN из колонтитула, и берёт
    только ту пару номеров, разница в которой совпадает с объёмом файла;
  * ответ модели раскладывается по слотам `articles` (третий язык уходит в
    metadata), а каждое поле сверяется с текстом файла ТЕМИ ЖЕ мерками, что
    потом применит автопроверка выпуска;
  * файл, который модель ещё не прочитала, статьёй не становится;
  * один и тот же файл дважды в загрузку не попадает (ключ — хеш содержимого);
  * диапазон страниц, уже занятый статьёй выпуска, блокирует строку ДО
    создания статьи — иначе автопроверка сочла бы это дублем и погасила номер;
  * созданная статья ложится в выбранный выпуск черновиком в очередь
    автопроверки, а не публикуется в обход неё.

Модель здесь не вызывается: ключей локально нет, а проверяем мы не качество
распознавания, а обработку ответа. `llm_layer` подменяется заглушкой.

Запуск: PYTHONPATH=. .venv/bin/python tests/verify_folder_import.py
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import delete, select

from src.core.config import settings
from src.domain import pdf_metadata
from src.domain.importing import ImportDomain, extract_status_of, file_key
from src.infrastructure.external import llm
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article,
    ImportItem,
    ImportJob,
    Issue,
    Journal,
    Profile,
)

GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"
results: list[bool] = []

TAG = "FOLDT"
ISSN = "2181-1415"


def check(name: str, got, want):
    ok = got == want
    results.append(ok)
    tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{tag}] {name}: got={got!r} want={want!r}")


def codes(problems: list[dict[str, Any]], severity: str | None = None) -> set[str]:
    return {
        p.get("code")
        for p in problems
        if severity is None or p.get("severity") == severity
    }


# --------------------------------------------------------------------------
# Синтетический PDF: текстовый слой без внешних файлов
# --------------------------------------------------------------------------


def _esc(line: str) -> bytes:
    out = line.encode("latin-1", "replace")
    for a, b in ((b"\\", b"\\\\"), (b"(", b"\\("), (b")", b"\\)")):
        out = out.replace(a, b)
    return out


def make_pdf(pages: list[str]) -> bytes:
    """Минимальный PDF с текстом Helvetica: по одной строке на строку списка."""
    objs: list[bytes] = [b"", b"", b""]  # 1 — каталог, 2 — дерево страниц, 3 — шрифт
    kids: list[int] = []
    for text in pages:
        parts = [b"BT", b"/F1 11 Tf", b"1 0 0 1 56 780 Tm", b"15 TL"]
        for line in text.split("\n"):
            parts.append(b"(" + _esc(line) + b") Tj T*")
        parts.append(b"ET")
        stream = b"\n".join(parts)
        objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        content_id = len(objs)
        objs.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources "
            b"<< /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content_id
        )
        kids.append(len(objs))
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids),
        len(kids),
    )
    objs[2] = (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>"
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref,
    )
    return bytes(out)


def article_pdf(
    *, title: str, title_uz: str, author: str, first: int, body: str
) -> bytes:
    """Три страницы вёрстки: шапка, текст, список литературы."""
    return make_pdf(
        [
            f"INTERNATIONAL JOURNAL OF APPLIED RESEARCH\nISSN {ISSN}\n"
            f"Volume 4, Issue 8\n{first}\nUDC 621.3\n{title}\n{title_uz}\n"
            f"{author}\nTashkent State Technical University\n"
            f"Abstract. {body}\nKeywords: logistics, digital, networks",
            f"INTERNATIONAL JOURNAL OF APPLIED RESEARCH\n{first + 1}\n{body}",
            f"INTERNATIONAL JOURNAL OF APPLIED RESEARCH\n{first + 2}\nReferences\n1. {body}",
        ]
    )


TITLE_1 = "DIGITAL TRANSFORMATION OF REGIONAL LOGISTICS NETWORKS"
TITLE_1_UZ = "MINTAQAVIY LOGISTIKA TARMOQLARINI RAQAMLI RIVOJLANTIRISH"
TITLE_2 = "ENERGY EFFICIENCY OF INDUSTRIAL PUMPING STATIONS"
TITLE_2_UZ = "SANOAT NASOS STANSIYALARINING ENERGIYA SAMARADORLIGI"
TITLE_3 = "MATHEMATICAL MODELLING OF IRRIGATION CANALS"
TITLE_3_UZ = "SUGORISH KANALLARINI MATEMATIK MODELLASHTIRISH"

PDF_1 = article_pdf(
    title=TITLE_1,
    title_uz=TITLE_1_UZ,
    author="Karimov Anvar Baxtiyorovich",
    first=45,
    body="The paper studies regional logistics networks and their digital maturity.",
)
PDF_2 = article_pdf(
    title=TITLE_2,
    title_uz=TITLE_2_UZ,
    author="Rasulova Nodira Ismoilovna",
    first=48,
    body="The paper evaluates energy efficiency of industrial pumping stations.",
)
# Тот же диапазон страниц, что у первой статьи, — на нём ловится конфликт.
PDF_3 = article_pdf(
    title=TITLE_3,
    title_uz=TITLE_3_UZ,
    author="Yusupov Shavkat Turgunovich",
    first=45,
    body="The paper describes mathematical modelling of irrigation canals.",
)


def model_answer(title: str, title_uz: str, author: str) -> dict[str, Any]:
    """Ответ модели в том виде, в каком его отдаёт схема SCHEMA."""
    return {
        "journal_title": "INTERNATIONAL JOURNAL OF APPLIED RESEARCH",
        "udc": "621.3",
        "versions": [
            {
                "language": "en",
                "title": title,
                "abstract": "The paper studies the subject in detail.",
                "keywords": ["logistics", "digital"],
            },
            {
                "language": "uz",
                "title": title_uz,
                "abstract": "Maqolada mavzu batafsil organilgan.",
                "keywords": ["logistika"],
            },
        ],
        "authors": [
            {
                "name": author,
                "affiliation": "Tashkent State Technical University",
                "email": "",
            }
        ],
        "notes": [],
    }


ANSWERS = {
    TITLE_1: model_answer(TITLE_1, TITLE_1_UZ, "Karimov Anvar Baxtiyorovich"),
    TITLE_2: model_answer(TITLE_2, TITLE_2_UZ, "Rasulova Nodira Ismoilovna"),
    TITLE_3: model_answer(TITLE_3, TITLE_3_UZ, "Yusupov Shavkat Turgunovich"),
}


async def fake_llm_layer(pages: list[str]) -> dict[str, Any]:
    """Заглушка модели: отвечает по заголовку, найденному в тексте страниц."""
    text = "\n".join(pages)
    for title, answer in ANSWERS.items():
        if title in text:
            return answer
    raise RuntimeError("заглушка не знает такого файла")


# --------------------------------------------------------------------------
# Слой без базы: regex, раскладка ответа модели, сверка
# --------------------------------------------------------------------------


def head_of(pdf: bytes) -> pdf_metadata.PdfHead:
    return pdf_metadata.read_head(pdf, max_pages=settings.IMPORT_EXTRACT_PAGES)


def check_regex() -> None:
    print("\n--- regex по колонтитулу ---")
    regex = pdf_metadata.regex_layer(head_of(PDF_1))
    check("страницы", regex["pages"], "45-47")
    check("том", regex["volume"], "4")
    check("номер", regex["issue"], "8")
    check("ISSN", regex["issns"], [ISSN])

    # Год отдельной строкой номером страницы не бывает: иначе «2024-2026»
    # уехало бы в страницы и пересеклось с чем угодно.
    years = pdf_metadata.read_head(
        make_pdf(["JOURNAL\n2024\ntitle", "JOURNAL\nbody", "JOURNAL\n2026\nend"]),
        max_pages=3,
    )
    check("годы не страницы", pdf_metadata.page_span(years), None)

    # Пара, не сходящаяся с объёмом файла, — это не страницы статьи.
    wrong = pdf_metadata.read_head(
        make_pdf(["JOURNAL\n45\ntitle", "JOURNAL\nbody", "JOURNAL\n99\nend"]), max_pages=3
    )
    check("несходящаяся пара отброшена", pdf_metadata.page_span(wrong), None)

    single = pdf_metadata.read_head(make_pdf(["JOURNAL\n12\nthesis"]), max_pages=3)
    check("одностраничный файл", pdf_metadata.page_span(single), (12, 12))


def check_to_fields() -> None:
    print("\n--- ответ модели → поля статьи ---")
    fields = pdf_metadata.to_fields(ANSWERS[TITLE_1])
    check("основной заголовок", fields["title"], TITLE_1)
    check("иностранный заголовок", fields["title_foreign"], TITLE_1_UZ)
    check("авторы строкой", fields["authors"], "Karimov Anvar Baxtiyorovich")
    check("аффилиация в pdf_meta", fields["pdf_meta"]["authors_detailed"][0]["affiliation"],
          "Tashkent State Technical University")
    check("УДК в pdf_meta", fields["pdf_meta"]["udc"], "621.3")

    # Три языка: иностранным берём английский, третий уезжает в metadata.
    three = pdf_metadata.to_fields(
        {
            "journal_title": "J",
            "udc": "",
            "versions": [
                {"language": "ru", "title": "Russkiy", "abstract": "", "keywords": []},
                {"language": "uz", "title": "Uzbekcha", "abstract": "", "keywords": []},
                {"language": "en", "title": "English", "abstract": "", "keywords": []},
            ],
            "authors": [{"name": "Petrov, P. P.", "affiliation": "", "email": ""}],
            "notes": [],
        }
    )
    check("основной — первый в файле", three["title"], "Russkiy")
    check("иностранный — английский", three["title_foreign"], "English")
    check(
        "третий язык в metadata",
        [v["title"] for v in three["pdf_meta"]["extra_languages"]],
        ["Uzbekcha"],
    )
    # Запятая в `articles.authors` разделяет авторов — внутри имени её быть не должно.
    check("запятая в имени убрана", three["authors"], "Petrov P. P.")


def check_verification() -> None:
    print("\n--- сверка полей с текстом файла ---")
    head = head_of(PDF_1)
    pages_text = pdf_metadata.pages_for_storage(head)
    regex = pdf_metadata.regex_layer(head)
    base = dict(
        pages_text=pages_text,
        total_pages=head.total_pages,
        extract={"status": pdf_metadata.EXTRACT_DONE},
        regex=regex,
        issue_volume="4",
        issue_number="8",
        journal_issns={ISSN},
    )
    good = {
        "title": TITLE_1,
        "authors": "Karimov Anvar Baxtiyorovich",
        "pages": "45-47",
    }
    check("чистая строка без замечаний", pdf_metadata.check_fields(good, **base), [])

    alien = pdf_metadata.check_fields({**good, "title": "COMPLETELY UNRELATED PAPER ABOUT BEEKEEPING"}, **base)
    check("чужой заголовок блокирует", codes(alien, pdf_metadata.BLOCKING), {"title_not_in_pdf"})

    confirmed = pdf_metadata.check_fields(
        {**good, "title": "COMPLETELY UNRELATED PAPER ABOUT BEEKEEPING", "confirmed": True},
        **base,
    )
    check("«всё верно» снимает блок", codes(confirmed, pdf_metadata.BLOCKING), set())
    check("но замечание остаётся", codes(confirmed), {"title_not_in_pdf"})

    volume_pages = pdf_metadata.check_fields({**good, "pages": "1-3"}, **base)
    check(
        "страницы = объём файла блокируют",
        codes(volume_pages, pdf_metadata.BLOCKING),
        {"pages_file_volume"},
    )

    no_authors = pdf_metadata.check_fields({**good, "authors": None}, **base)
    check("без авторов блок", codes(no_authors, pdf_metadata.BLOCKING), {"authors_required"})

    foreign = pdf_metadata.check_fields(good, **{**base, "journal_issns": {"1234-5678"}})
    check("чужой ISSN — предупреждение", codes(foreign, pdf_metadata.WARNING), {"foreign_issn"})
    check("и не блокирует", codes(foreign, pdf_metadata.BLOCKING), set())

    scan = pdf_metadata.check_fields(
        {"pages": "45-47"},
        **{**base, "pages_text": [], "extract": {"status": pdf_metadata.EXTRACT_NO_TEXT}},
    )
    check("скан — предупреждение", "scan" in codes(scan, pdf_metadata.WARNING), True)

    mismatch = pdf_metadata.check_fields(good, **{**base, "issue_number": "9"})
    check("номер выпуска разошёлся", codes(mismatch, pdf_metadata.WARNING), {"issue_mismatch"})


# --------------------------------------------------------------------------
# Сквозной путь: файл → распознавание → статья
# --------------------------------------------------------------------------


async def _sweep(db) -> None:
    journal_ids = (
        await db.execute(select(Journal.id).where(Journal.name.like(f"{TAG} %")))
    ).scalars().all()
    if not journal_ids:
        return
    issue_ids = (
        await db.execute(select(Issue.id).where(Issue.journal_id.in_(journal_ids)))
    ).scalars().all()
    for stmt in (
        delete(Article).where(Article.issue_id.in_(issue_ids)),
        delete(ImportJob).where(ImportJob.journal_id.in_(journal_ids)),
        delete(Issue).where(Issue.id.in_(issue_ids)),
        delete(Journal).where(Journal.id.in_(journal_ids)),
    ):
        await db.execute(stmt)
    await db.commit()


async def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    imports = ImportDomain()
    pdf_metadata.llm_layer = fake_llm_layer  # type: ignore[assignment]
    llm.configured = lambda: True  # type: ignore[assignment]

    async with AsyncSessionLocal() as db:
        await _sweep(db)
        user_id = (await db.execute(select(Profile.id).limit(1))).scalars().first()
        if user_id is None:
            print(f"{RED}в базе нет ни одного профиля — тест пропущен{RESET}")
            return 1

        journal = Journal(
            name=f"{TAG} journal {suffix}",
            slug=f"foldt-{suffix}",
            issn=ISSN,
            type="journal",
        )
        db.add(journal)
        await db.flush()
        issue = Issue(
            journal_id=journal.id, title=f"{TAG} issue", year=2025, volume="4", issue="8"
        )
        db.add(issue)
        await db.commit()
        journal_id, issue_id = journal.id, issue.id

        check_regex()
        check_to_fields()
        check_verification()

        print("\n--- приём файлов ---")
        job = await imports.create_job(
            db,
            journal_id=journal_id,
            created_by=user_id,
            source_type="folder",
            source_ref="Выпуск 4(8)",
            params={"issue_id": issue_id},
        )
        item1 = await imports.add_folder_file(
            db, job, filename="a1.pdf", content=PDF_1, url="https://r2/a1.pdf",
            head=head_of(PDF_1),
        )
        item2 = await imports.add_folder_file(
            db, job, filename="a2.pdf", content=PDF_2, url="https://r2/a2.pdf",
            head=head_of(PDF_2),
        )
        check("ключ строки — хеш файла", item1.source_key, file_key(PDF_1))
        check("страницы из regex", (item1.parsed or {}).get("pages"), "45-47")
        check("год от выпуска", (item1.parsed or {}).get("publication_year"), 2025)
        check("том от выпуска", (item1.parsed or {}).get("volume"), "4")
        check("пока распознаётся — замечаний нет", item1.problems, [])
        check("статус распознавания", extract_status_of(item1),
              pdf_metadata.EXTRACT_QUEUED)
        await db.refresh(job)
        check("в сводке — счётчик распознавания", (job.totals or {}).get("extracting"), 2)

        print("\n--- тот же файл второй раз ---")
        again = await imports.folder_precheck(db, job, PDF_1)
        check("precheck нашёл строку", again.id if again else None, item1.id)
        same = await imports.add_folder_file(
            db, job, filename="a1-copy.pdf", content=PDF_1, url="https://r2/a1-copy.pdf",
            head=head_of(PDF_1),
        )
        check("новой строки не завели", same.id, item1.id)

        print("\n--- нераспознанное в статьи не уходит ---")
        early = await imports.apply(db, job)
        check("создано статей", early["created"], 0)

        print("\n--- распознавание моделью ---")
        counts = await imports.extract_folder(db, job)
        check("распознано файлов", counts["extracted"], 2)
        await db.refresh(item1)
        await db.refresh(item2)
        check("заголовок из шапки", (item1.parsed or {}).get("title"), TITLE_1)
        check("иностранный заголовок", (item1.parsed or {}).get("title_foreign"), TITLE_1_UZ)
        check("авторы", (item1.parsed or {}).get("authors"), "Karimov Anvar Baxtiyorovich")
        check("замечаний нет", item1.problems, [])
        check("строка готова", item1.status, "pending")
        check("вторая строка готова", item2.status, "pending")
        rows, total = await imports.folder_items(db, job.id)
        check("список отдаёт все строки", total, 2)
        check(
            "и статус распознавания",
            {row["extract_status"] for row in rows},
            {pdf_metadata.EXTRACT_DONE},
        )

        print("\n--- создание статей ---")
        applied = await imports.apply(db, job)
        check("создано статей", applied["created"], 2)
        check("без ошибок", applied["failed"], 0)
        article_ids = await imports.created_article_ids(db, job.id)
        check("обе строки связаны со статьёй", len(article_ids), 2)
        check("список статей в порядке загрузки", article_ids[0], item1.article_id)
        article = (
            await db.execute(select(Article).where(Article.id == item1.article_id))
        ).scalars().first()
        check("статья в выбранном выпуске", article.issue_id, issue_id)
        check("название", article.title, TITLE_1)
        check("страницы", article.pages, "45-47")
        check("PDF прикреплён", article.pdf, "https://r2/a1.pdf")
        check("аффилиации в metadata", bool((article.meta or {}).get("pdf_meta")), True)
        check("автор правки — редактор", article.admin_id, user_id)
        # Публикация идёт через ту же очередь, что у формы админки: сама фича
        # не должна быть дырой в обход автопроверки.
        check("черновик", article.published, not settings.REVIEW_ACTIVE)
        check(
            "в очереди автопроверки",
            ((article.meta or {}).get("ai_review") or {}).get("status"),
            "queued" if settings.REVIEW_ACTIVE else None,
        )

        print("\n--- занятые страницы ---")
        item3 = await imports.add_folder_file(
            db, job, filename="a3.pdf", content=PDF_3, url="https://r2/a3.pdf",
            head=head_of(PDF_3),
        )
        await imports.extract_folder(db, job)
        await db.refresh(item3)
        check("совпадение диапазона блокирует", item3.status, "invalid")
        check("замечание о конфликте", "page_conflict" in codes(item3.problems), True)
        check(
            "и оно блокирующее",
            codes(item3.problems, pdf_metadata.BLOCKING),
            {"page_conflict"},
        )
        # Правка страниц снимает конфликт — и статья создаётся.
        await imports.set_item(db, item3.id, parsed={"pages": "51-53"})
        await imports.revalidate(db, job)
        await db.refresh(item3)
        check("после правки строка готова", item3.status, "pending")

        print("\n--- модель не настроена ---")
        llm.configured = lambda: False  # type: ignore[assignment]
        job2 = await imports.create_job(
            db,
            journal_id=journal_id,
            created_by=user_id,
            source_type="folder",
            params={"issue_id": issue_id},
        )
        scan = make_pdf(["JOURNAL\n60\n", "JOURNAL\n61\n", "JOURNAL\n62\n"])
        item4 = await imports.add_folder_file(
            db, job2, filename="a4.pdf", content=scan, url="https://r2/a4.pdf",
            head=head_of(scan),
        )
        await imports.extract_folder(db, job2)
        await db.refresh(item4)
        check("статус — модель выключена", extract_status_of(item4),
              pdf_metadata.EXTRACT_NO_MODEL)
        check("строка ждёт редактора", item4.status, "invalid")
        check("и объясняет почему", codes(item4.problems),
              {"required", "extract_off", "authors_required"})
        empty = await imports.apply(db, job2)
        check("пустая строка статьёй не стала", empty["created"], 0)

        left = (
            await db.execute(select(ImportItem).where(ImportItem.job_id == job2.id))
        ).scalars().all()
        check("строка осталась в загрузке", len(left), 1)

        await _sweep(db)

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

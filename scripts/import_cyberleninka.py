"""Забрать волюм журнала с КиберЛенинки в задачу импорта.

Зачем отдельный скрипт, если есть мастер импорта: обход тут длинный и рваный.
В `oai_dc` КиберЛенинки нет ни года, ни выпуска — только название, автор и
ссылка, поэтому год выясняется заходом на страницу каждой статьи, а сам
источник после нескольких тысяч запросов подряд закрывается страницей «Вы
точно человек?». Мастер такой прогон переживёт (состояние в БД), но нажимать
«продолжить» руками полдня никто не станет.

Что делает скрипт:

* идёт по набору помесячно — окно `from`/`until` по дате ЗАГРУЗКИ записи в
  КиберЛенинку, а не по году статьи (года в OAI нет). Волюм 2023 года залит
  туда в начале 2024-го, поэтому узкое окно экономит тысячи запросов;
* оставляет только нужный год публикации и только нужный журнал (набор OAI
  бывает шире журнала);
* напоровшись на антибота, ждёт и повторяет тот же месяц, а не падает;
* складывает кандидатов в обычную задачу импорта — дальше владелец смотрит их
  в мастере (`/admin/import`) и применяет, PDF качаются на этом шаге.

Темп задаётся снаружи: IMPORT_MIN_INTERVAL=3 (пауза между обращениями к
источнику в секундах, см. `src/infrastructure/external/safe_fetch.py`).

Пример:
    IMPORT_MIN_INTERVAL=3 python scripts/import_cyberleninka.py \
        --set journal_37143 --journal-id 10 --year 2023 \
        --journal-title "Inter education & global study" \\
        --start 2024-01-01 --window 7
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta

from sqlalchemy import select

from src.domain.importing import ImportDomain, ImportError_
from src.infrastructure.external.oai import OaiError
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import ImportJob, Profile

BASE_URL = "https://cyberleninka.ru/oai"


def windows(start: date, end: date, days: int):
    """Окна по `days` дней подряд, включительно с обоих концов."""
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=days - 1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


async def get_job(db, args) -> ImportJob:
    if args.job_id:
        job = (
            await db.execute(select(ImportJob).where(ImportJob.id == args.job_id))
        ).scalar_one()
        return job
    # Автор задачи обязателен (FK на profiles) и определяет, от чьего имени она
    # видна в мастере. Импорт — услуга владельца платформы, поэтому по умолчанию
    # владелец и есть автор.
    created_by = args.created_by
    if not created_by:
        created_by = (
            await db.execute(select(Profile.id).where(Profile.role == "owner").limit(1))
        ).scalar_one()
    job = ImportJob(
        journal_id=args.journal_id,
        created_by=created_by,
        source_type="oai",
        source_ref=BASE_URL,
        status="draft",
        params={},
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", dest="set_spec", required=True, help="setSpec журнала в OAI")
    parser.add_argument("--journal-id", type=int, required=True, help="журнал на нашей платформе")
    parser.add_argument("--year", type=int, required=True, help="год публикации, который забираем")
    parser.add_argument("--journal-title", default=None, help="название журнала у источника")
    parser.add_argument("--start", required=True, help="начало обхода, YYYY-MM-DD")
    parser.add_argument("--until", default=None, help="конец обхода, YYYY-MM-DD (иначе — до пустых окон)")
    parser.add_argument(
        "--window",
        type=int,
        default=7,
        help="ширина окна в днях. Проход берёт не больше 2000 записей "
        "(MAX_ITEMS_PER_JOB), и на широком окне хвост просто теряется: "
        "у КиберЛенинки в одном январе их больше двух тысяч",
    )
    parser.add_argument(
        "--stop-after-empty",
        type=int,
        default=8,
        help="остановиться после стольких окон подряд без попаданий",
    )
    parser.add_argument("--retries", type=int, default=6, help="попыток на окно при блокировке")
    parser.add_argument("--backoff", type=int, default=1800, help="пауза после блокировки, сек")
    parser.add_argument("--job-id", type=int, default=None, help="дописать в существующую задачу")
    parser.add_argument("--created-by", default=None, help="автор задачи (UUID профиля); по умолчанию владелец")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    # Без явного конца идём до сегодня, полагаясь на счётчик пустых окон.
    end = date.fromisoformat(args.until) if args.until else date.today()

    domain = ImportDomain()
    async with AsyncSessionLocal() as db:
        job = await get_job(db, args)
        print(f"задача импорта #{job.id}, журнал {args.journal_id}", flush=True)

        empty_streak = 0
        first = args.job_id is None
        total_kept = 0

        for window_start, window_end in windows(start, end, args.window):
            if args.until is None and empty_streak >= args.stop_after_empty:
                print(f"{empty_streak} окон подряд без попаданий — останавливаюсь", flush=True)
                break

            date_from, date_until = window_start.isoformat(), window_end.isoformat()
            job.params = {
                "base_url": BASE_URL,
                "set": args.set_spec,
                "deep": True,
                "year": args.year,
                "journal_title": args.journal_title,
                "from": date_from,
                "until": date_until,
                # Закладка от прошлого окна к новому отношения не имеет.
                "resume_token": None,
            }
            await db.commit()

            for attempt in range(1, args.retries + 1):
                try:
                    # resume=True со второго окна: иначе разбор стирает
                    # кандидатов, собранных прошлыми окнами.
                    await domain.load_oai(db, job, resume=not first)
                    break
                except (OaiError, ImportError_) as e:
                    text = str(e)
                    if "вы не робот" not in text and "не отдал ни одной записи" not in text:
                        print(f"{date_from}: {text}", flush=True)
                        break
                    if "не отдал ни одной записи" in text:
                        break  # пустое окно — это нормально, не блокировка
                    print(
                        f"{date_from}: источник закрылся антиботом, попытка {attempt}/{args.retries}, "
                        f"жду {args.backoff} с",
                        flush=True,
                    )
                    if attempt == args.retries:
                        print("не дождался — прекращаю", flush=True)
                        return 1
                    await asyncio.sleep(args.backoff)

            first = False
            scan = (job.params or {}).get("scan") or {}
            kept = scan.get("kept", 0)
            total_kept += kept
            print(
                f"{date_from}..{date_until}: записей {scan.get('records', 0)}, "
                f"взято {kept}, не тот год {scan.get('skipped_by_year', 0)}, "
                f"не тот журнал {scan.get('skipped_by_journal', 0)}, "
                f"страница не далась {scan.get('landing_failed', 0)}",
                flush=True,
            )
            if scan.get("records", 0) >= 2000:
                print(
                    f"  ВНИМАНИЕ: окно упёрлось в потолок 2000 записей — сузьте --window, "
                    f"хвост окна не просмотрен",
                    flush=True,
                )
            empty_streak = empty_streak + 1 if kept == 0 else 0

        print(f"итого кандидатов за прогон: {total_kept}", flush=True)
        print(f"дальше — мастер импорта, задача #{job.id}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

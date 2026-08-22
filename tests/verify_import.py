"""Проверка импорта архивов (`/import`) на живом API.

Сквозной сценарий глазами клиента: создать задачу → залить таблицу → увидеть
превью с дубликатом и битой строкой → загрузить PDF → применить → проверить,
что статьи созданы ЧЕРНОВИКАМИ, разложены по выпускам и не видны в витрине.

Запуск (API и БД должны быть подняты):
    PYTHONPATH=. .venv/bin/python tests/verify_import.py \
        --api http://localhost:8000 --email demo@researcher.uz --password ...
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.error
import urllib.request
import uuid

API = "http://localhost:8000"
_BOUNDARY = "----researcher-verify-import"


def req(
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    token: str | None = None,
    multipart: tuple[str, str, bytes, str] | None = None,
    expect: int | None = None,
) -> tuple[int, dict | list | str]:
    url = f"{API}{path}"
    data: bytes | None = None
    headers: dict[str, str] = {"Accept": "application/json"}
    if multipart:
        field, filename, content, ctype = multipart
        parts = [
            f"--{_BOUNDARY}\r\n".encode(),
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {ctype}\r\n\r\n".encode(),
            content,
            f"\r\n--{_BOUNDARY}--\r\n".encode(),
        ]
        data = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={_BOUNDARY}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
        if expect is not None and e.code == expect:
            return e.code, payload
        return e.code, payload


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail else ""))
    return ok


def sample_pdf() -> bytes:
    body = b"BT /F1 12 Tf 72 720 Td (import test) Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs)+1}\n".encode() + b"0000000000 65535 f \n"
    for off in offs:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{x}\n".encode() + b"%%EOF\n"
    return bytes(out)


def main() -> int:
    global API
    p = argparse.ArgumentParser()
    p.add_argument("--api", default=API)
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--journal-id", type=int, help="по умолчанию первый журнал, где есть права")
    args = p.parse_args()
    API = args.api.rstrip("/")

    failures = 0

    _, tokens = req("/auth/login", method="POST", body={"email": args.email, "password": args.password})
    token = (tokens or {}).get("access_token")
    if not token:
        print("Не удалось войти:", tokens)
        return 1

    journal_id = args.journal_id
    if journal_id is None:
        _, mine = req("/admin/my-journals", token=token)
        if not mine:
            print("У пользователя нет журналов")
            return 1
        journal_id = mine[0]
    print(f"Журнал: {journal_id}\n")

    # --- права -------------------------------------------------------------
    print("Права:")
    code, _ = req("/import/jobs", method="POST", body={"journal_id": journal_id}, expect=401)
    failures += not check("без токена — 401/403", code in (401, 403), f"код {code}")

    # --- создание задачи ---------------------------------------------------
    print("\nЗадача:")
    code, job = req("/import/jobs", method="POST", body={"journal_id": journal_id}, token=token)
    failures += not check("создана", code == 201 and job.get("status") == "draft", f"код {code}")
    job_id = job.get("id")
    if not job_id:
        print("Дальше идти некуда:", job)
        return 1

    # --- таблица -----------------------------------------------------------
    # Уникальный маркер в названиях: прогон не должен спотыкаться о статьи,
    # созданные предыдущим прогоном (они станут дубликатами).
    tag = uuid.uuid4().hex[:8]
    csv_text = (
        "title,authors,year,volume,issue,pages,doi,pdf_filename\n"
        f"Импортная статья один {tag},Иванов И.И.; Петров П.П.,2019,3,1,5-12,https://doi.org/10.5555/{tag}.1,one.pdf\n"
        f"Импортная статья два {tag},Сидоров С.,2019,3,1,13-20,,two.pdf\n"
        f"Импортная статья три {tag},Каримов А.,2020,4,2,,,\n"
        ",Без названия,2020,,,,,\n"          # invalid: пустой заголовок
        f"Импортная статья один {tag},Иванов И.И.,2019,3,1,5-12,https://doi.org/10.5555/{tag}.1,one.pdf\n"  # дубль в файле
    )
    code, res = req(
        f"/import/jobs/{job_id}/table",
        method="POST",
        token=token,
        multipart=("file", "archive.csv", csv_text.encode("utf-8"), "text/csv"),
    )
    failures += not check("таблица разобрана", code == 200, f"код {code}")
    if code == 200:
        failures += not check("дубль внутри файла отброшен", res.get("parsed") == 4, f"parsed={res.get('parsed')}")
        totals = (res.get("job") or {}).get("totals", {})
        failures += not check("битая строка помечена invalid", totals.get("invalid") == 1, str(totals))
        failures += not check("готовых к импорту 3", totals.get("pending") == 3, str(totals))

    # --- PDF ---------------------------------------------------------------
    print("\nФайлы:")
    code, pdf_res = req(
        f"/import/jobs/{job_id}/files",
        method="POST",
        token=token,
        multipart=("file", "one.pdf", sample_pdf(), "application/pdf"),
    )
    pdf_ok = code == 200 and pdf_res.get("matched")
    failures += not check("PDF привязан по имени файла", pdf_ok, f"код {code}, {pdf_res}")
    if code == 503:
        print("       (R2 не сконфигурирован — проверка PDF пропущена)")
        failures -= 1

    code, _ = req(
        f"/import/jobs/{job_id}/files",
        method="POST",
        token=token,
        multipart=("file", "fake.pdf", b"<html>not a pdf</html>", "application/pdf"),
        expect=400,
    )
    failures += not check("не-PDF отклонён", code == 400, f"код {code}")

    # --- превью ------------------------------------------------------------
    print("\nПревью:")
    code, page = req(f"/import/jobs/{job_id}/items?limit=10", token=token)
    items = page.get("items", []) if code == 200 else []
    failures += not check("список кандидатов отдан", code == 200 and page.get("total") == 4, f"код {code}")
    invalid = [i for i in items if i["status"] == "invalid"]
    failures += not check("у битой строки есть причина", bool(invalid and invalid[0]["problems"]), "")

    # правка: снимаем строку без названия
    if invalid:
        code, _ = req(f"/import/items/{invalid[0]['id']}", method="PATCH", body={"status": "skipped"}, token=token)
        failures += not check("строку можно снять вручную", code == 200, f"код {code}")

    # --- применение --------------------------------------------------------
    print("\nИмпорт:")
    ready = [i["id"] for i in items if i["status"] == "pending"]
    code, job = req(f"/import/jobs/{job_id}/apply", method="POST", body={"item_ids": ready}, token=token)
    failures += not check("применено", code == 200 and job.get("status") == "done", f"код {code}, {job.get('status')}")
    totals = job.get("totals", {})
    failures += not check("создано 3 статьи", totals.get("created") == 3, str(totals))

    code, page = req(f"/import/jobs/{job_id}/items?status=created", token=token)
    created = page.get("items", []) if code == 200 else []
    article_ids = [i["article_id"] for i in created if i.get("article_id")]
    failures += not check("у строк проставлен article_id", len(article_ids) == 3, str(len(article_ids)))

    # --- что получилось в журнале -----------------------------------------
    print("\nРезультат в журнале:")
    code, listing = req(f"/articles/?journal_id={journal_id}&limit=200", token=token)
    by_id = {a["id"]: a for a in listing.get("items", [])} if code == 200 else {}
    mine = [by_id[a] for a in article_ids if a in by_id]
    failures += not check("статьи видны в админке журнала", len(mine) == 3, str(len(mine)))
    failures += not check(
        "созданы черновиками", all(a.get("published") is False for a in mine), ""
    )
    failures += not check(
        "разложены по двум выпускам",
        len({a.get("issue_id") for a in mine}) == 2,
        str({a.get("issue_id") for a in mine}),
    )
    if pdf_ok:
        with_pdf = [a for a in mine if a.get("pdf")]
        failures += not check("PDF доехал до статьи", len(with_pdf) == 1, str(len(with_pdf)))

    # --- витрина -----------------------------------------------------------
    print("\nВитрина:")
    code, feed = req("/articles/?limit=200&published=true")
    feed_ids = {a["id"] for a in feed.get("items", [])} if code == 200 else set()
    failures += not check(
        "черновиков нет в публичной ленте", not (feed_ids & set(article_ids)), ""
    )

    # --- повторный разбор того же файла ------------------------------------
    print("\nПовторная заливка:")
    code, job2 = req("/import/jobs", method="POST", body={"journal_id": journal_id}, token=token)
    job2_id = job2.get("id")
    code, res2 = req(
        f"/import/jobs/{job2_id}/table",
        method="POST",
        token=token,
        multipart=("file", "archive.csv", csv_text.encode("utf-8"), "text/csv"),
    )
    totals2 = (res2.get("job") or {}).get("totals", {}) if code == 200 else {}
    failures += not check(
        "уже импортированное распознано как дубликаты",
        totals2.get("duplicate") == 3,
        str(totals2),
    )

    print("\n" + ("ВСЁ ХОРОШО" if failures == 0 else f"ПРОВАЛОВ: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

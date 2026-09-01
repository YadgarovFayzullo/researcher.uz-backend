"""OAI-PMH: что эндпоинт закрыт и что через него не утекает лишнее.

Проверяем не столько протокол, сколько границы доступа — ради них он и закрыт:
  * без ключа, с чужим, отозванным, просроченным ключом и с неразрешённого
    IP — 401, причём с одинаковым текстом (по ответу нельзя понять, угадан ли
    ключ);
  * `allowed_journal_ids` держит клиента внутри его журналов: и в ListRecords,
    и при попытке подставить чужой set, и при подсунутом чужом resumptionToken
    (в токене лежат фильтры, а не права);
  * наружу не уходит неопубликованное и демо-журнал;
  * ссылка на PDF появляется только у ключа с include_fulltext;
  * `updated_at` (датировка записи для инкрементального сбора) двигается на
    правку метаданных и НЕ двигается на просмотр — иначе харвестеры тянули бы
    базу заново каждый день;
  * resumptionToken переживает круг и не теряет микросекунды: с точностью до
    секунды записи одной и той же секунды приезжали бы по второму разу.

Нужна живая БД (как verify_authz.py). Фикстуры создаются и удаляются за собой.

    PYTHONPATH=. .venv/bin/python tests/verify_oai.py
"""
from __future__ import annotations

import asyncio
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import httpx
from httpx import ASGITransport
from sqlalchemy import delete, select, update

from src.core.oai_access import hash_token, ip_allowed, new_token
from src.domain.oai import Selector, decode_token, encode_token
from src.infrastructure.persistence.db import AsyncSessionLocal
from src.infrastructure.persistence.models import (
    Article,
    Issue,
    Journal,
    OaiClient,
)
from src.main import app

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results: list[bool] = []

NS = {
    "o": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  [{PASS if ok else FAIL}] {name}: got={got!r} want={want!r}")


def ids(xml: str) -> set[str]:
    return {e.text for e in ET.fromstring(xml).findall(".//o:header/o:identifier", NS)}


def error_code(xml: str) -> str | None:
    el = ET.fromstring(xml).find("o:error", NS)
    return el.get("code") if el is not None else None


def dc_values(xml: str, tag: str) -> list[str]:
    return [e.text for e in ET.fromstring(xml).findall(f".//dc:{tag}", NS)]


async def main() -> None:
    tag = uuid.uuid4().hex[:8]
    tokens: dict[str, str] = {}

    async with AsyncSessionLocal() as db:
        # ---------------------------------------------------------- фикстуры
        j_a = Journal(name=f"OAI A {tag}", slug=f"oai-a-{tag}", issn="1111-1111")
        j_b = Journal(name=f"OAI B {tag}", slug=f"oai-b-{tag}")
        j_demo = Journal(name=f"OAI demo {tag}", slug=f"oai-demo-{tag}", meta={"demo": True})
        db.add_all([j_a, j_b, j_demo])
        await db.flush()

        i_a = Issue(journal_id=j_a.id, year=2026, volume="1", issue="2")
        i_b = Issue(journal_id=j_b.id, year=2026)
        i_demo = Issue(journal_id=j_demo.id, year=2026)
        db.add_all([i_a, i_b, i_demo])
        await db.flush()

        def article(issue, name, *, published=True):
            return Article(
                issue_id=issue.id,
                title=f"Article {name} {tag}",
                slug=f"oai-{name}-{tag}",
                authors="Иванов И.И., Петров П.П.",
                keywords="один, два",
                annotation="Аннотация",
                pdf=f"https://files.example/{name}.pdf",
                pages="10-20",
                data=date(2026, 1, 15),
                published=published,
            )

        a_pub = article(i_a, "a-pub")
        a_draft = article(i_a, "a-draft", published=False)
        b_pub = article(i_b, "b-pub")
        d_pub = article(i_demo, "d-pub")
        db.add_all([a_pub, a_draft, b_pub, d_pub])
        await db.flush()

        # ---------------------------------------------------------- клиенты
        def client(name, **kw):
            token = new_token()
            tokens[name] = token
            db.add(OaiClient(name=f"{name} {tag}", token_hash=hash_token(token), **kw))

        client("full", include_fulltext=True)
        client("meta")
        client("only_a", allowed_journal_ids=[j_a.id])
        client("disabled", enabled=False)
        client("expired", expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        client("wrong_ip", ip_allowlist=["10.0.0.0/8"])
        client("right_ip", ip_allowlist=["127.0.0.0/8"])
        await db.commit()

        oai_ids = {
            "a_pub": f"oai:researcher.uz:article/{a_pub.id}",
            "a_draft": f"oai:researcher.uz:article/{a_draft.id}",
            "b_pub": f"oai:researcher.uz:article/{b_pub.id}",
            "d_pub": f"oai:researcher.uz:article/{d_pub.id}",
        }

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver", timeout=60
        ) as http:

            async def ask(who: str | None, **params):
                if who is not None:
                    params["key"] = tokens[who]
                return await http.get("/oai", params=params)

            # ------------------------------------------------ допуск
            print("\n— допуск —")
            anon = await ask(None, verb="Identify")
            check("без ключа", anon.status_code, 401)

            wrong = await http.get("/oai", params={"verb": "Identify", "key": "nope"})
            check("чужой ключ", wrong.status_code, 401)
            check(
                "ответ не выдаёт, существует ли ключ",
                wrong.text == anon.text,
                True,
            )
            check("отозванный", (await ask("disabled", verb="Identify")).status_code, 401)
            check("просроченный", (await ask("expired", verb="Identify")).status_code, 401)
            check("не тот IP", (await ask("wrong_ip", verb="Identify")).status_code, 401)
            check("разрешённый IP", (await ask("right_ip", verb="Identify")).status_code, 200)
            check("ip_allowlist: пустой = любой", ip_allowed("8.8.8.8", []), True)
            check("ip_allowlist: битый CIDR не пускает", ip_allowed("8.8.8.8", ["не сеть"]), False)

            hdr = await http.get(
                "/oai",
                params={"verb": "Identify"},
                headers={"Authorization": f"Bearer {tokens['meta']}"},
            )
            check("ключ заголовком Authorization", hdr.status_code, 200)

            # ------------------------------------------------ что видно
            print("\n— видимость записей —")
            got = await ask("full", verb="ListIdentifiers", metadataPrefix="oai_dc", set=f"journal:{j_a.slug}")
            check("опубликованная отдаётся", oai_ids["a_pub"] in ids(got.text), True)
            check("черновик не отдаётся", oai_ids["a_draft"] in ids(got.text), False)

            demo = await ask("full", verb="ListIdentifiers", metadataPrefix="oai_dc", set=f"journal:{j_demo.slug}")
            check("демо-журнал: пусто", error_code(demo.text), "noRecordsMatch")

            one = await ask("full", verb="GetRecord", metadataPrefix="oai_dc", identifier=oai_ids["d_pub"])
            check("демо-статья по прямому id", error_code(one.text), "idDoesNotExist")
            draft = await ask("full", verb="GetRecord", metadataPrefix="oai_dc", identifier=oai_ids["a_draft"])
            check("черновик по прямому id", error_code(draft.text), "idDoesNotExist")

            # ------------------------------------------------ полные тексты
            print("\n— полные тексты —")
            rec_full = await ask("full", verb="GetRecord", metadataPrefix="oai_dc", identifier=oai_ids["a_pub"])
            rec_meta = await ask("meta", verb="GetRecord", metadataPrefix="oai_dc", identifier=oai_ids["a_pub"])
            check(
                "include_fulltext=true: PDF есть",
                any(".pdf" in v for v in dc_values(rec_full.text, "identifier")),
                True,
            )
            check(
                "include_fulltext=false: PDF нет",
                any(".pdf" in v for v in dc_values(rec_meta.text, "identifier")),
                False,
            )
            check(
                "адрес статьи канонический (/uz/)",
                any("/uz/article/" in v for v in dc_values(rec_meta.text, "identifier")),
                True,
            )
            check("авторы разобраны", len(dc_values(rec_meta.text, "creator")), 2)

            # ------------------------------------------------ ограничение по журналам
            print("\n— ограничение по журналам —")
            mine = await ask("only_a", verb="ListIdentifiers", metadataPrefix="oai_dc")
            check("свой журнал виден", oai_ids["a_pub"] in ids(mine.text), True)
            check("чужой журнал не виден", oai_ids["b_pub"] in ids(mine.text), False)

            foreign = await ask(
                "only_a", verb="ListIdentifiers", metadataPrefix="oai_dc", set=f"journal:{j_b.slug}"
            )
            check("чужой set не открывает доступ", error_code(foreign.text), "noRecordsMatch")

            sets = await ask("only_a", verb="ListSets")
            set_specs = [e.text for e in ET.fromstring(sets.text).findall(".//o:setSpec", NS)]
            check("ListSets: свой журнал", f"journal:{j_a.slug}" in set_specs, True)
            check("ListSets: чужого нет", f"journal:{j_b.slug}" in set_specs, False)

            # Токен, выданный ключу без ограничений, отдаём ограниченному.
            wide = await ask("full", verb="ListIdentifiers", metadataPrefix="oai_dc")
            wide_token = ET.fromstring(wide.text).find(".//o:resumptionToken", NS).text
            if wide_token:
                replayed = await ask("only_a", verb="ListIdentifiers", resumptionToken=wide_token)
                leaked = oai_ids["b_pub"] in ids(replayed.text)
                check("чужой resumptionToken не расширяет права", leaked, False)
            else:
                check("чужой resumptionToken не расширяет права", "нет второй страницы", "нет второй страницы")

            # ------------------------------------------------ датировка
            print("\n— датировка записи —")
            before = (await db.execute(select(Article.updated_at).where(Article.id == a_pub.id))).scalar()
            await db.execute(
                update(Article).where(Article.id == a_pub.id).values(views_count=Article.views_count + 1)
            )
            await db.commit()
            after_view = (await db.execute(select(Article.updated_at).where(Article.id == a_pub.id))).scalar()
            check("просмотр не двигает дату", after_view == before, True)

            await db.execute(
                update(Article).where(Article.id == a_pub.id).values(title=f"Правленый {tag}")
            )
            await db.commit()
            after_edit = (await db.execute(select(Article.updated_at).where(Article.id == a_pub.id))).scalar()
            check("правка двигает дату", after_edit > before, True)

            since = (after_edit - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            fresh = await ask("full", verb="ListIdentifiers", metadataPrefix="oai_dc", **{"from": since})
            check("правленая попадает в инкремент", oai_ids["a_pub"] in ids(fresh.text), True)

            # ------------------------------------------------ протокол
            print("\n— протокол —")
            check("badVerb", error_code((await ask("meta", verb="Nope")).text), "badVerb")
            check(
                "неизвестный metadataPrefix",
                error_code((await ask("meta", verb="ListRecords", metadataPrefix="jats")).text),
                "cannotDisseminateFormat",
            )
            check(
                "битая дата",
                error_code((await ask("meta", verb="ListRecords", metadataPrefix="oai_dc", **{"from": "вчера"})).text),
                "badArgument",
            )
            check(
                "битый resumptionToken",
                error_code((await ask("meta", verb="ListRecords", resumptionToken="???")).text),
                "badResumptionToken",
            )
            echoed = await ask("meta", verb="ListMetadataFormats")
            check("ключ не попадает в эхо запроса", tokens["meta"] in echoed.text, False)

        # ------------------------------------------------ токен: круг
        print("\n— resumptionToken —")
        moment = datetime(2026, 5, 1, 12, 30, 45, 123456, tzinfo=timezone.utc)
        selector = Selector(frm=moment, until=moment, set_spec="type:article")
        round_trip = decode_token(encode_token(selector, (moment, 42), 7, "oai_dc"))
        check("курсор не теряет микросекунды", round_trip[1], (moment, 42))
        check("фильтры переживают круг", round_trip[0].set_spec, "type:article")
        check("completeListSize переживает круг", round_trip[2], 7)

        # ------------------------------------------------ уборка
        await db.execute(delete(Article).where(Article.slug.like(f"oai-%-{tag}")))
        await db.execute(delete(Issue).where(Issue.journal_id.in_([j_a.id, j_b.id, j_demo.id])))
        await db.execute(delete(Journal).where(Journal.id.in_([j_a.id, j_b.id, j_demo.id])))
        await db.execute(delete(OaiClient).where(OaiClient.name.like(f"%{tag}")))
        await db.commit()

    ok = sum(results)
    print(f"\n{ok}/{len(results)} проверок пройдено")
    raise SystemExit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())

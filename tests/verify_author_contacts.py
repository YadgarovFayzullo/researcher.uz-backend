"""Почта авторов из текста статьи: разбор и привязка к подписям.

Стережём:
  * адрес из хвостового блока «Сведения об авторах» находится вместе с ORCID;
  * кириллические двойники в домене («gmail.сom») не теряют адрес;
  * ящики редакции (info@, editor@) не попадают в авторские контакты;
  * адрес привязывается по ORCID, по похожему имени рядом и по правилу
    «один автор — один адрес»;
  * двух однофамильцев рядом с одним адресом не разбираем — адрес не ставим;
  * уже проставленный адрес не перетирается без overwrite.

Запуск: PYTHONPATH=. .venv/bin/python tests/verify_author_contacts.py
"""
from __future__ import annotations

from src.domain.author_contacts import Contact, _match, normalize_email, parse_contacts
from src.infrastructure.persistence.models import ArticleAuthor

GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"
results: list[bool] = []


def check(name: str, got, want):
    ok = got == want
    results.append(ok)
    tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{tag}] {name}: got={got!r} want={want!r}")


def row(rid: str, name: str, orcid: str | None = None, email: str | None = None) -> ArticleAuthor:
    return ArticleAuthor(id=rid, author_name=name, orcid=orcid, email=email, article_id=1,
                         author_order=1, from_claim=False)


TAIL = """
Maqola matni shu yerda tugaydi.

Mualliflar haqida ma'lumot:
Yadgarov Fayzullo Nodirovich — Buxoro davlat universiteti dotsenti.
[Манзил: Ўзбекистон, Бухоро, Ибн Сино 1/Б-уй], [Address: Bukhara, Ibn Sina 1/B]
E-mail: f.yadgarov@gmail.com ORCID: 0000-0002-1825-0097
Karimova Dilnoza Anvarovna — TDPU tadqiqotchisi.
E-mail: dilnoza.karimova@mail.ru.
Jurnal tahririyati: info@jusr.uz, editor@jusr.uz
"""


def test_parse() -> None:
    print("\n--- разбор текста ---")
    contacts = parse_contacts(TAIL)
    check("нашлись только авторские адреса",
          [c.email for c in contacts],
          ["f.yadgarov@gmail.com", "dilnoza.karimova@mail.ru"])
    check("ORCID подхвачен рядом с адресом", contacts[0].orcid, "0000-0002-1825-0097")
    check("имя автора попало в окружение",
          any("Yadgarov" in n for n in contacts[0].names), True)
    check("точка после адреса отрезана", contacts[1].email.endswith("mail.ru"), True)

    check("кириллический домен исправлен",
          [c.email for c in parse_contacts("E-mail: abror@gmail.сom")],
          ["abror@gmail.com"])
    check("повтор адреса не дублируется",
          len(parse_contacts("a@gmail.com текст a@gmail.com")), 1)
    check("текста без адресов хватает", parse_contacts("нет почты"), [])


def test_match() -> None:
    print("\n--- привязка к подписям ---")
    contacts = parse_contacts(TAIL)

    rows = [row("r1", "F.N. Yadgarov", orcid="0000-0002-1825-0097"), row("r2", "D.A. Karimova")]
    check("по ORCID и по имени",
          {k: v.email for k, v in _match(rows, contacts).items()},
          {"r1": "f.yadgarov@gmail.com", "r2": "dilnoza.karimova@mail.ru"})

    one = [row("solo", "Кто-то Совсем Другой")]
    check("один автор и один адрес — связываем напрямую",
          {k: v.email for k, v in _match(one, [Contact("x@gmail.com", None, ())]).items()},
          {"solo": "x@gmail.com"})

    twins = [row("t1", "Yadgarov F."), row("t2", "Yadgarov N.")]
    check("однофамильцы с общим адресом остаются без него",
          _match(twins, [Contact("y@gmail.com", None, ("Yadgarov",))]), {})

    # ORCID чужой строки не должен утаскивать адрес к ней.
    wrong = [row("w1", "Karimova D.", orcid="0000-0002-1825-0097")]
    check("адрес уходит к строке с этим ORCID",
          {k: v.email for k, v in _match(wrong, [Contact("z@gmail.com", "0000-0002-1825-0097", ())]).items()},
          {"w1": "z@gmail.com"})


SPACED = """
Mualliflar haqida: Junaydullayev Mels Abdurasulovich, Buxoro davlat universiteti.
[address: Uzbekistan, Bukhara M.Iqbol 11]; ORCID: https://orcid.org/0000-0002-7256-4588;
E-mail: junaydullayevmels @gmail. com
"""


def test_layout() -> None:
    print("\n--- вёрстка PDF ---")
    contacts = parse_contacts(SPACED)
    check("пробелы внутри адреса убираются",
          [c.email for c in contacts], ["junaydullayevmels@gmail.com"])
    check("ORCID из ссылки orcid.org", contacts[0].orcid, "0000-0002-7256-4588")
    # «Ergashov — botirergashov258@gmail.com»: тире прилипало к имени ящика, и
    # письмо такому адресу отскакивало (19.09, Resend).
    check("тире перед адресом срезается",
          normalize_email("-botirergashov258@gmail.com"), "botirergashov258@gmail.com")
    check("точка в конце имени ящика срезается",
          normalize_email("anisa.0804.@gmail.com"), "anisa.0804@gmail.com")
    check("дефис внутри имени остаётся",
          normalize_email("o-zod@gmail.com"), "o-zod@gmail.com")
    check("приклеенный телефон отрезается",
          normalize_email("+998997465973sanjarnomozov2002@gmail.com"),
          "sanjarnomozov2002@gmail.com")
    check("цифры года в начале имени не телефон",
          normalize_email("2004zilola@gmail.com"), "2004zilola@gmail.com")

    rows = [row("s1", "M.A. Junaydullayev")]
    got = _match(rows, contacts)
    check("подпись получает и адрес, и ORCID",
          (got["s1"].email, got["s1"].orcid),
          ("junaydullayevmels@gmail.com", "0000-0002-7256-4588"))

    check("ссылка на youtube не считается адресом",
          parse_contacts("см. https://www.youtube.com/@gunaui4933"), [])


def main() -> int:
    test_parse()
    test_match()
    test_layout()
    passed = sum(results)
    print(f"\n{passed}/{len(results)} проверок пройдено")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

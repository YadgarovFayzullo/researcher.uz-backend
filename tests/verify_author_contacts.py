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

from src.domain.author_contacts import Contact, _match, parse_contacts
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
          _match(rows, contacts),
          {"r1": "f.yadgarov@gmail.com", "r2": "dilnoza.karimova@mail.ru"})

    one = [row("solo", "Кто-то Совсем Другой")]
    check("один автор и один адрес — связываем напрямую",
          _match(one, [Contact("x@gmail.com", None, ())]), {"solo": "x@gmail.com"})

    twins = [row("t1", "Yadgarov F."), row("t2", "Yadgarov N.")]
    check("однофамильцы с общим адресом остаются без него",
          _match(twins, [Contact("y@gmail.com", None, ("Yadgarov",))]), {})

    # ORCID чужой строки не должен утаскивать адрес к ней.
    wrong = [row("w1", "Karimova D.", orcid="0000-0002-1825-0097")]
    check("адрес уходит к строке с этим ORCID",
          _match(wrong, [Contact("z@gmail.com", "0000-0002-1825-0097", ())]),
          {"w1": "z@gmail.com"})


def main() -> int:
    test_parse()
    test_match()
    passed = sum(results)
    print(f"\n{passed}/{len(results)} проверок пройдено")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

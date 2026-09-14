"""Проверка рассылки авторам (без сети и без БД).

Подпись ссылки отписки, экранирование подстановок, отсутствие недоставленных
переменных в шаблонах.

    PYTHONPATH=. .venv/bin/python tests/verify_outreach.py
"""
from src.domain import outreach

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results = []


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  [{PASS if ok else FAIL}] {name}: got={got!r} want={want!r}")


print("\n— подпись отписки —")
e, t = outreach.unsubscribe_token("Author@Gmail.com ")
check("верная подпись → адрес в нижнем регистре", outreach.verify_unsubscribe(e, t), "author@gmail.com")
check("чужая подпись", outreach.verify_unsubscribe(e, t[:-2] + "xx"), None)
e2, _ = outreach.unsubscribe_token("other@gmail.com")
check("подпись от другого адреса", outreach.verify_unsubscribe(e2, t), None)
check("мусор в e", outreach.verify_unsubscribe("%%%", t), None)
check("пустые параметры", outreach.verify_unsubscribe("", ""), None)

print("\n— шаблоны —")
row = {
    "slug": "karimov-a-b",
    "name": 'Karimov <b>"A"</b>',
    "works": 3,
    "article": "Очень длинное название статьи про экономику регионов и цифровизацию",
    "journal": "Iqtisodiyot & taraqqiyot",
    "article_slug": "ekonomika-regionov",
}
for campaign in outreach.CAMPAIGNS:
    letter = outreach.render(campaign, "author@gmail.com", row)
    check(f"{campaign}: имя экранировано в HTML", "<b>" in letter["html"], False)
    check(f"{campaign}: & экранирован", "& taraqqiyot" in letter["html"], False)
    check(f"{campaign}: в тексте нет незаменённых $", "$" in letter["text"], False)
    check(f"{campaign}: ссылка на страницу автора",
          "/uz/author/karimov-a-b" in letter["html"] and "/uz/author/karimov-a-b" in letter["text"], True)
    check(f"{campaign}: ссылка отписки в обеих частях",
          letter["unsubscribe"] in letter["text"] and "/outreach/unsubscribe?e=" in letter["html"], True)
    if campaign != "uz-2":
        check(f"{campaign}: название статьи — ссылкой на неё",
              '<a href="https://researcher.uz/uz/article/ekonomika-regionov"' in letter["html"]
              and "/uz/article/ekonomika-regionov" in letter["text"], True)

subj = outreach.render("uz-1b", "author@gmail.com", row)["subject"]
check("1Б: название статьи в теме обрезано", "…" in subj and len(subj) < 110, True)
check("тема детерминирована по адресу",
      outreach.subject_for("uz-1a", "x@gmail.com", {"name": "N"}),
      outreach.subject_for("uz-1a", "X@gmail.com", {"name": "N"}))

print("\n— подпись вебхука Resend (Svix) —")
import base64 as _b64  # noqa: E402
import hashlib as _hashlib  # noqa: E402
import hmac as _hmac  # noqa: E402
import time as _time  # noqa: E402

key = b"test-webhook-key-0123456789"
secret = "whsec_" + _b64.b64encode(key).decode()
body = b'{"type":"email.bounced","data":{"to":["a@b.uz"],"bounce":{"type":"Permanent"}}}'
ts = str(int(_time.time()))
sig = _b64.b64encode(_hmac.new(key, f"msg_1.{ts}.".encode() + body, _hashlib.sha256).digest()).decode()
check("верная подпись", outreach.verify_svix(secret, "msg_1", ts, f"v1,{sig}", body), True)
check("несколько подписей, верна вторая", outreach.verify_svix(secret, "msg_1", ts, f"v1,AAAA v1,{sig}", body), True)
check("изменённое тело", outreach.verify_svix(secret, "msg_1", ts, f"v1,{sig}", body + b" "), False)
check("чужой секрет", outreach.verify_svix("whsec_" + _b64.b64encode(b"other").decode(), "msg_1", ts, f"v1,{sig}", body), False)
check("другой svix-id", outreach.verify_svix(secret, "msg_2", ts, f"v1,{sig}", body), False)
old = str(int(ts) - 3600)
old_sig = _b64.b64encode(_hmac.new(key, f"msg_1.{old}.".encode() + body, _hashlib.sha256).digest()).decode()
check("верная подпись, но час назад", outreach.verify_svix(secret, "msg_1", old, f"v1,{old_sig}", body), False)
check("нет заголовков", outreach.verify_svix(secret, None, ts, f"v1,{sig}", body), False)
check("нет секрета", outreach.verify_svix(None, "msg_1", ts, f"v1,{sig}", body), False)

print("\n— проверка адреса без сети —")
from src.domain import email_check  # noqa: E402

check("точка перед @", email_check.syntax_problem("anisa.sharipova0804.@gmail.com") is not None, True)
check("двойная точка", email_check.syntax_problem("x..y@gmail.com") is not None, True)
check("нормальный адрес", email_check.syntax_problem("eshkaraevsadridin@gmail.com"), None)
check("gmail.ru — опечатка", email_check.domain_problem("kibriev1991@gmail.ru") is not None, True)
check("info@ — служебный", email_check.domain_problem("info@tersu.uz") is not None, True)
check("mail.uz — живой сервис", email_check.domain_problem("oaliyarov@mail.uz"), None)

check("телефон приклеен к адресу",
      email_check.syntax_problem("+998997465973sanjarnomozov2002@gmail.com") is not None, True)
check("обрезанное имя ящика", email_check.syntax_problem("7@gmail.com") is not None, True)
check("короткое, но настоящее имя", email_check.syntax_problem("lola@tersu.uz"), None)

print("\n— имена капсом —")
from src.domain.author_names import unshout  # noqa: E402

check("капс → обычный регистр", unshout("AMANULLAYEV ABDUNABI ABDUMO'MINOVICH"),
      "Amanullayev Abdunabi Abdumo'minovich")
check("инициалы через точку", unshout("A.S.YO'LDOSHALIYEV"), "A.S.Yo'ldoshaliyev")
check("отчество через апостроф", unshout("SAFAROV SARVARJON CHORI O'G'LI"),
      "Safarov Sarvarjon Chori O'g'li")
check("смешанное написание не трогаем", unshout("Hasanova Saodat Muhammadi qizi"),
      "Hasanova Saodat Muhammadi qizi")
# Вариант темы выбирается по адресу и может быть без имени, поэтому имя
# проверяем в приветствии, а тему — только на отсутствие капса.
shout = outreach.render("uz-1a", "a@gmail.com", {**row, "name": "SHONAZAROVA SEVARA"})
check("приветствие без капса", "Assalomu alaykum, Shonazarova Sevara!" in shout["text"], True)
check("в теме нет капса", "SHONAZAROVA" in shout["subject"], False)

print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)

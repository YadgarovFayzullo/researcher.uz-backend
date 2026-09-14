"""Проверка адреса до отправки — всё, что можно узнать без порта 25.

Существует ли сам ящик, отсюда не выяснить: это SMTP-запрос к серверу
получателя, а DigitalOcean закрывает исходящий 25-й порт. Зато ловится то, что
в первой рассылке дало гарантированные отказы: битый синтаксис из разбора PDF
(`имя.@gmail.com`), домены без почты (`gmail.ru`), опечатки в домене.

Возвращает причину отказа строкой или None, если адрес годен к отправке.
Сетевой сбой DNS адрес НЕ бракует: «не узнали» — не повод выбросить автора.
"""
from __future__ import annotations

import re

import dns.asyncresolver
import dns.exception
import dns.resolver

# dot-atom по RFC 5322 без экзотики: точка не первой, не последней, не дважды.
LOCAL_RE = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

# Опечатки, встреченные в списке авторов и типичные для региона. Значение —
# вероятный правильный домен: подставлять его молча нельзя (это уже другой
# адрес), но в отчёте он подсказывает, что случилось.
TYPO_DOMAINS = {
    "gmail.ru": "gmail.com", "gmail.uz": "gmail.com", "gmail.co": "gmail.com",
    "gmail.con": "gmail.com", "gmail.cm": "gmail.com", "gmial.com": "gmail.com",
    "gmai.com": "gmail.com", "gmal.com": "gmail.com", "gamil.com": "gmail.com",
    "gnail.com": "gmail.com", "gmaill.com": "gmail.com", "googlemail.ru": "gmail.com",
    "mail.ry": "mail.ru", "mali.ru": "mail.ru", "mai.ru": "mail.ru", "mail.ri": "mail.ru",
    "yandex.ry": "yandex.ru", "yandx.ru": "yandex.ru", "inbox.ry": "inbox.ru",
    "list.ry": "list.ru", "bk.ry": "bk.ru",
}

# Одноразовые ящики: автор статьи на таком не живёт, письмо уйдёт в пустоту.
DISPOSABLE_DOMAINS = {
    "mailinator.com", "10minutemail.com", "guerrillamail.com", "tempmail.com",
    "temp-mail.org", "yopmail.com", "trashmail.com", "sharklasers.com",
    "getnada.com", "dispostable.com", "maildrop.cc",
}

# Адрес редакции или отдела, а не человека: писать туда «ваша страница автора»
# бессмысленно, а жалобу получить легко.
ROLE_LOCALPART = re.compile(
    r"^(info|admin|office|support|editor|editorial|redaksiya|redaktsiya|journal|"
    r"contact|mail|post|noreply|no-reply|rektor|rector|kafedra|dekanat|priyomnaya|"
    r"secretary|kotib|nashriyot|webmaster|postmaster|abuse)\d*$"
)


# Телефон, склеенный с адресом при извлечении текста из PDF:
# «+998997465973sanjarnomozov2002@gmail.com». Формально адрес допустим («+» и
# цифры в имени ящика разрешены), но такого ящика нет — это отказ.
GLUED_PHONE = re.compile(r"^\+?\d{9,}(?=[a-z])")


def syntax_problem(email: str) -> str | None:
    email = (email or "").strip().lower()
    local, sep, domain = email.rpartition("@")
    if not sep or not local or not domain:
        return "нет @ или пустая часть адреса"
    phone = GLUED_PHONE.match(local)
    if phone:
        return f"к адресу приклеен телефон (вероятно {local[phone.end():]}@{domain})"
    # «v@mail.ru», «7@gmail.com»: начало адреса отрезано при извлечении из PDF.
    # Порог в 2 символа, а не 4: «lola@», «niso@» — настоящие имена.
    if len(local) <= 2:
        return "имя ящика из 1–2 символов — скорее всего обрезано при извлечении"
    if len(email) > 254 or len(local) > 64:
        return "адрес длиннее допустимого"
    if not LOCAL_RE.match(local):
        return "недопустимые символы или точки в имени ящика"
    if not DOMAIN_RE.match(domain):
        return "некорректный домен"
    return None


def domain_problem(email: str) -> str | None:
    local, _, domain = (email or "").strip().lower().rpartition("@")
    if domain in TYPO_DOMAINS:
        return f"опечатка в домене: {domain} (скорее всего {TYPO_DOMAINS[domain]})"
    if domain in DISPOSABLE_DOMAINS:
        return f"одноразовый домен {domain}"
    if ROLE_LOCALPART.match(local):
        return f"служебный адрес ({local}@)"
    return None


async def accepts_mail(domain: str, cache: dict[str, bool | None]) -> bool | None:
    """Есть ли у домена куда доставлять почту: MX, а без него — A-запись
    (RFC 5321 разрешает доставку на сам домен). None — DNS не ответил."""
    if domain in cache:
        return cache[domain]
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = 5
    result: bool | None
    try:
        answer = await resolver.resolve(domain, "MX")
        # «Null MX» (RFC 7505): домен явно заявляет, что почту не принимает.
        result = not all(str(r.exchange) in (".", "") for r in answer)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        try:
            await resolver.resolve(domain, "A")
            result = True
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            result = False
        except dns.exception.DNSException:
            result = None
    except dns.exception.DNSException:
        result = None
    cache[domain] = result
    return result


async def check(email: str, mx_cache: dict[str, bool | None]) -> str | None:
    """Причина не слать письмо или None."""
    problem = syntax_problem(email) or domain_problem(email)
    if problem:
        return problem
    domain = email.strip().lower().rpartition("@")[2]
    if await accepts_mail(domain, mx_cache) is False:
        return f"домен {domain} не принимает почту (нет MX и A)"
    return None

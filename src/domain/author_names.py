"""Разбор и нормализация авторских имён — основа карточек авторов.

Задача модуля: из того, как автор записан в конкретной статье, получить ключ
личности, одинаковый для всех её написаний. В базе один человек встречается как
«Xalilova Z.F.», «Xalilova Zilola Farhodovna» и «Халилова З.Ф.» — без общего
ключа у него будет три карточки вместо одной.

Политика склейки СОЗНАТЕЛЬНО консервативная: ключ = фамилия + набор инициалов.
Более агрессивное правило (только фамилия + первый инициал) слило бы однофамильцев
с совпадающим инициалом в одного человека, и в карточке оказались бы чужие
работы — ошибка, которую автор увидит и не простит. Обратная цена: у части людей
будет по 2-3 карточки, пока их не объединят руками (claim владельцем или
owner-инструмент слияния).
"""
from __future__ import annotations

import json
import re
import unicodedata

# Разные апострофы из узбекской латиницы: o‘g‘li, o’g’li, o'g'li — одно и то же.
APOSTROPHES = "’‘'`ʻʼ´"

# Окончания отчеств. Их роль здесь одна: опознать порядок «Фамилия Имя Отчество»
# (узбекский и русский), где фамилия стоит ПЕРВОЙ, — в отличие от «Ism Familiya».
PATRONYMIC = re.compile(
    r"(ovich|evich|ovna|evna|ович|евич|овна|евна)$", re.IGNORECASE
)

# Те же отчества, но отдельными словами: «Dilshodjon qizi», «Sherzod o'g'li».
# Сами по себе они не имя и инициала не дают — иначе «Ergasheva Nigora Dilshodjon
# qizi» получила бы лишний инициал «q» и не склеилась с «Ergasheva N.D.».
STANDALONE_SUFFIX = {
    "qizi", "kizi", "qizi", "o'g'li", "o'gli", "ogli", "ugli", "oglu",
    "қизи", "кизи", "кызы", "ўғли", "ўгли", "угли", "оглы",
}

# Диграфы узбекской латиницы: в «Rasulov Sh.M» это инициалы Ш. и М., а не слово.
DIGRAPHS = ("sh", "ch", "kh", "zh", "gh", "ts", "ya", "yu", "yo", "o'", "g'")

# Типичные окончания фамилий — нужны, чтобы отличить «Choriyeva, Dilnoza»
# (фамилия и имя ОДНОГО человека) от «Каримов, Петров» (два автора).
SURNAME_END = re.compile(
    r"(ov|ova|ev|eva|yev|yeva|iev|ieva|in|ina|zoda|zade|skiy|skaya|sky|"
    r"ов|ова|ев|ева|ёв|ёва|ин|ина|ский|ская|заде|зода)$",
    re.IGNORECASE,
)

# Кириллица → латиница: один и тот же человек публикуется на обоих алфавитах,
# и «Мамаражабов Жахонгир» обязан попасть в ту же карточку, что «Mamarajabov
# Jahongir». Транслитерация грубая (не ГОСТ) и служит только ключом, наружу
# не показывается.
CYR2LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "",
    "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "ғ": "g", "қ": "q", "ҳ": "h", "ў": "o",
}


def translit(value: str) -> str:
    return "".join(CYR2LAT.get(ch, ch) for ch in value)


def clean(raw: str) -> str:
    """Убрать разнобой апострофов, пробелов и висящую пунктуацию."""
    text = unicodedata.normalize("NFC", raw or "")
    for ch in APOSTROPHES:
        text = text.replace(ch, "'")
    return re.sub(r"\s+", " ", text).strip(" ,;.")


def looks_like_surname(word: str) -> bool:
    return bool(SURNAME_END.search(word.strip()))


def split_authors(text: str) -> list[str]:
    """Строка `articles.authors` → список имён.

    В базе три формата разом: JSON-массив (наследие импорта), перечисление через
    запятую или точку с запятой и одиночное имя. Плюс экспорт со старого OJS
    пишет «Фамилия , Имя» — там запятая разделяет НЕ авторов.
    """
    text = (text or "").strip()
    if not text:
        return []

    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return [clean(str(x)) for x in parsed if clean(str(x))]

    if ";" not in text and "," in text:
        pairs = _split_surname_given_pairs(text)
        if pairs:
            return pairs

    parts = re.split(r"[;\n]", text) if (";" in text or "\n" in text) else text.split(",")
    out: list[str] = []
    for part in parts:
        part = clean(part)
        if not part:
            continue
        # «Xalilova, Z.F.» — запятая отделила инициалы, а не следующего автора.
        if out and _is_initials_only(part):
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part)
    return out


def _is_initials_only(fragment: str) -> bool:
    letters = fragment.replace(".", "").replace(" ", "")
    return len(letters) <= 3 and bool(re.fullmatch(r"[^\W\d_]+", letters, re.UNICODE))


def _split_surname_given_pairs(text: str) -> list[str] | None:
    """«Turopova , Nigora, Absalomov , Shaydulla» → два автора, а не четыре.

    Формат опознаём по двум признакам сразу: фрагменты — одиночные слова без
    инициалов, и в каждой паре первое слово похоже на фамилию, а второе нет.
    У «Каримов, Петров» второе условие не выполняется, и это честно читается
    как два автора.
    """
    frags = [clean(x) for x in text.split(",") if clean(x)]
    if len(frags) < 2 or len(frags) % 2 != 0:
        return None
    if not all(len(f.split()) == 1 and "." not in f for f in frags):
        return None
    if not all(
        looks_like_surname(frags[i]) and not looks_like_surname(frags[i + 1])
        for i in range(0, len(frags), 2)
    ):
        return None
    return [f"{frags[i]} {frags[i + 1]}" for i in range(0, len(frags), 2)]


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[\s.]+", clean(name).lower()) if t]


def _is_initial(token: str) -> bool:
    return len(token) == 1 or (len(token) == 2 and token in DIGRAPHS)


def _initial_of(token: str) -> str:
    """«Sh.» и «Shavkat» должны давать одну и ту же букву ключа."""
    return token[0]


def identity_key(name: str) -> str:
    """Ключ личности. Пустая строка = карточку заводить нельзя.

    Пустой ключ отдаётся для имён из одного слова («Sevara», «Jalolov»): за таким
    написанием стоят разные люди, и общая карточка была бы ложным слиянием.
    """
    raw = _tokens(name)
    if not raw:
        return ""

    # Порядок определяем ДО выбрасывания суффиксов: у «Ergasheva Nigora
    # Dilshodjon qizi» признак порядка — это самое «qizi».
    uzbek_order = raw[-1] in STANDALONE_SUFFIX or bool(PATRONYMIC.search(raw[-1]))

    parts = [translit(t) for t in raw if t not in STANDALONE_SUFFIX]
    parts = [t for t in parts if t and t not in STANDALONE_SUFFIX]
    if not parts:
        return ""

    words = [t for t in parts if not _is_initial(t)]
    initials = [_initial_of(t) for t in parts if _is_initial(t)]

    if uzbek_order and words:
        surname, rest = words[0], words[1:]
    elif len(words) == 1 and initials:
        surname, rest = words[0], []
    elif len(words) == 1:
        return ""
    elif len(words) >= 2:
        # Порядок неизвестен («Kudratbek Makhmudov» против «Makhmudov
        # Kudratbek») — не угадываем, а берём отсортированный набор слов: оба
        # написания дают один ключ, чужого в карточку это не приносит.
        key = "+".join(sorted(words))
        return key + ("|" + ".".join(sorted(initials)) if initials else "")
    else:
        return ""

    all_initials = sorted(initials + [_initial_of(w) for w in rest])
    return f"{surname}|{'.'.join(all_initials)}"


def _interpretations(name: str) -> set[tuple[str, frozenset[str]]]:
    """Все допустимые чтения подписи: (фамилия, набор инициалов).

    Порядок слов в подписи не задан («Makhmudov Kudratbek» и «Kudratbek
    Makhmudov» — один человек), поэтому фамилией по очереди считается каждое
    слово, а буквы остальных идут в инициалы. Написание без инициалов вообще
    («Jalolov») чтений не даёт: за ним стоят разные люди.
    """
    raw = _tokens(name)
    if not raw:
        return set()
    uzbek_order = raw[-1] in STANDALONE_SUFFIX or bool(PATRONYMIC.search(raw[-1]))
    parts = [translit(t) for t in raw if t not in STANDALONE_SUFFIX]
    parts = [t for t in parts if t and t not in STANDALONE_SUFFIX]

    words = [t for t in parts if not _is_initial(t)]
    initials = [_initial_of(t) for t in parts if _is_initial(t)]
    if not words:
        return set()
    # Отчество в конце выдаёт порядок «Фамилия Имя Отчество» — гадать не нужно.
    candidates = [0] if (uzbek_order or len(words) == 1) else range(len(words))

    out: set[tuple[str, frozenset[str]]] = set()
    for i in candidates:
        letters = frozenset(
            initials + [_initial_of(w) for j, w in enumerate(words) if j != i]
        )
        if letters:
            out.add((words[i], letters))
    return out


def may_be_same_person(a: str, b: str) -> bool:
    """Могут ли два написания быть одним человеком.

    СОЗНАТЕЛЬНО мягче `identity_key`: совпадает фамилия, а набор инициалов
    одного — подмножество другого («Fayzullo Yadgarov» и «F.N. Yadgarov»).
    Ключ личности так склеивать нельзя — он общий на всю платформу, и под
    «Yadgarov F.» окажется десяток разных людей.

    Поэтому применять эту проверку можно ТОЛЬКО в пределах одной статьи и
    только когда подходящая подпись ровно одна: там список авторов короткий,
    человек сам заявил своё авторство, а однофамилец с другим инициалом
    («Yadgarov N.») отсекается требованием подмножества.
    """
    mine = _interpretations(a)
    theirs = _interpretations(b)
    return any(
        surname == other_surname and (letters <= other or other <= letters)
        for surname, letters in mine
        for other_surname, other in theirs
    )


def display_name(variants: list[str]) -> str:
    """Как показывать карточку: самое длинное написание из встреченных.

    Полная форма («Xalilova Zilola Farhodovna») информативнее инициалов, а при
    равной длине берём лексикографически первое — чтобы имя не прыгало от
    запуска к запуску.
    """
    cleaned = [clean(v) for v in variants if clean(v)]
    if not cleaned:
        return ""
    return sorted(cleaned, key=lambda v: (-len(v), v))[0]


def slug_for(key: str, display: str) -> str:
    """Человекочитаемый адрес карточки: /author/<slug>.

    Строится из ключа, а не из показываемого имени: ключ уже нормализован и
    стабилен, а написание может смениться при появлении более полной формы —
    менять URL из-за этого нельзя.
    """
    base = translit(key.replace("|", "-").replace("+", "-").replace(".", "-"))
    base = re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-")
    base = re.sub(r"-{2,}", "-", base)
    return base or "author"


def unshout(name: str) -> str:
    """«AMANULLAYEV ABDUNABI ABDUMO'MINOVICH» → «Amanullayev Abdunabi Abdumo'minovich».

    Трогает только имя, набранное целиком капсом: смешанное написание автор
    выбрал сам. Каждая часть между пробелом, точкой и дефисом — с заглавной, а
    не `str.title()`: тот поднимает букву после апострофа («Abdumo'Minovich»,
    «O'G'Li»), а в узбекской латинице апостроф — часть буквы.
    """
    if not name or not name.isupper():
        return name
    return re.sub(r"[^\s.\-]+", lambda m: m.group()[:1] + m.group()[1:].lower(), name)

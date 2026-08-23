"""Отпечатки текста для поиска заимствований.

Метод — шинглы с winnowing (тот же приём, что в MOSS): текст режется на
перекрывающиеся цепочки слов, каждая хешируется, и из каждого окна хешей
берётся минимальный. Это даёт устойчивость к перестановкам и вставкам, но
хранит не все шинглы подряд, а примерно каждый k-й — иначе на 50 тысячах
статей отпечатки перестанут помещаться в базу.

Почему именно слова, а не символы: тексты трёхъязычные (узбекская латиница,
кириллица, английский), и посимвольные n-граммы дали бы кучу ложных
совпадений на общей лексике.

Нормализация здесь важнее алгоритма. Один и тот же узбекский текст пишут через
`oʻ`, `o'`, `o‘` и `o`; кириллицу и латиницу мешают в одном абзаце. Без
приведения к общему виду копия своего же текста в другой раскладке не
опознается.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

# Длина шингла в словах. Короче — много случайных совпадений на устойчивых
# оборотах («в результате проведённого исследования»), длиннее — перестановка
# пары слов ломает обнаружение.
SHINGLE_SIZE = 6
# Окно winnowing: из каждых WINDOW хешей оставляем минимальный. Гарантия
# метода — любое совпадение длиной ≥ SHINGLE_SIZE + WINDOW - 1 слов будет
# поймано.
WINDOW = 4
# Ниже этого числа слов текст не проверяем: на тезисах в пару абзацев процент
# заимствований — шум.
MIN_WORDS = 120

# Апострофы узбекской латиницы: ʻ ʼ ' ‘ ’ ` ´ — все приводим к одному виду,
# а потом выбрасываем: «oʻzbek», «o'zbek» и «ozbek» должны совпасть.
_APOSTROPHES = dict.fromkeys(map(ord, "ʻʼ'‘’`´ʹ"), None)

# Кириллица → латиница для узбекских текстов: одна и та же статья ходит в двух
# графиках, и без свёртки копия в другой раскладке не находится.
_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "қ": "q", "ў": "o", "ғ": "g", "ҳ": "h",
}

_WORD_SPLIT = re.compile(r"[^\w]+", re.UNICODE)


def normalize_words(text: str) -> list[str]:
    """Текст → список нормализованных слов (порядок сохраняется)."""
    if not text:
        return []
    lowered = unicodedata.normalize("NFKC", text).lower().translate(_APOSTROPHES)
    folded = "".join(_CYR_TO_LAT.get(ch, ch) for ch in lowered)
    return [w for w in _WORD_SPLIT.split(folded) if w]


def _hash(words: tuple[str, ...]) -> int:
    """64-битный хеш шингла.

    Берём blake2b с digest_size=8: встроенный hash() рандомизируется от запуска
    к запуску (PYTHONHASHSEED), и отпечатки, посчитанные вчера, не совпали бы с
    сегодняшними.
    """
    digest = hashlib.blake2b("\x1f".join(words).encode("utf-8"), digest_size=8).digest()
    # Postgres bigint знаковый — держим значение в диапазоне int64.
    return int.from_bytes(digest, "big", signed=True)


def fingerprints(text: str) -> list[tuple[int, int]]:
    """Отпечатки текста: список (хеш, позиция первого слова шингла).

    Позиция нужна отчёту: по ней восстанавливается совпавший фрагмент, чтобы
    редактор видел не только процент, но и сам текст.
    """
    words = normalize_words(text)
    if len(words) < SHINGLE_SIZE:
        return []

    shingles = [
        (_hash(tuple(words[i : i + SHINGLE_SIZE])), i)
        for i in range(len(words) - SHINGLE_SIZE + 1)
    ]

    picked: dict[int, int] = {}
    for start in range(0, max(1, len(shingles) - WINDOW + 1)):
        window = shingles[start : start + WINDOW]
        if not window:
            break
        # При равных хешах берём правый — так соседние окна чаще выбирают один
        # и тот же шингл, и отпечаток получается компактнее.
        best = min(window, key=lambda pair: (pair[0], -pair[1]))
        picked.setdefault(best[0], best[1])

    return sorted(picked.items(), key=lambda pair: pair[1])


def total_shingles(text: str) -> int:
    """Сколько шинглов в тексте всего — знаменатель для процента совпадения."""
    words = normalize_words(text)
    return max(0, len(words) - SHINGLE_SIZE + 1)


def extract_fragment(text: str, word_index: int, length: int = SHINGLE_SIZE * 3) -> str:
    """Кусок исходного текста вокруг позиции — для показа в отчёте."""
    words = re.split(r"(\s+)", text)
    only_words = [i for i, w in enumerate(words) if w.strip()]
    if word_index >= len(only_words):
        return ""
    start = only_words[max(0, word_index - 2)]
    end_index = min(len(only_words) - 1, word_index + length)
    end = only_words[end_index]
    return "".join(words[start : end + 1]).strip()

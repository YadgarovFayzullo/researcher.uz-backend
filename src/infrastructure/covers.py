"""Генерация обложки издания из первой страницы PDF.

Зачем: на публичных страницах (издатель, каталог изданий) карточка без
`cover_image` рисовала обложку в браузере через pdf.js — то есть качала весь
PDF ради одной страницы. На странице издателя с 42 изданиями это больше сотни
мегабайт трафика. Здесь та же картинка делается один раз на сервере и кладётся
в R2, после чего карточка тянет обычный JPEG на десятки килобайт.

Рендерер — pypdfium2 (Apache-2.0/BSD): в отличие от PyMuPDF не тянет за собой
AGPL и не требует системных пакетов вроде poppler.
"""
from __future__ import annotations

import io
import logging
import time

from slugify import slugify

from src.infrastructure.storage import (
    StorageNotConfigured,
    key_from_url,
    public_url,
    storage,
)

logger = logging.getLogger(__name__)

# Ширина обложки в пикселях. 320 хватает для карточки в сетке (в вёрстке она
# около 300 CSS-px), ретина добирает резкость за счёт JPEG-качества.
COVER_WIDTH = 320
JPEG_QUALITY = 82


def render_first_page(pdf_bytes: bytes) -> bytes | None:
    """Первая страница PDF → JPEG. None, если отрисовать не удалось."""
    try:
        import pypdfium2 as pdfium
        from PIL import Image  # noqa: F401  (нужен для to_pil)
    except ImportError:
        logger.warning("pypdfium2/Pillow не установлены — обложка не создана")
        return None

    doc = None
    try:
        doc = pdfium.PdfDocument(pdf_bytes)
        if len(doc) == 0:
            return None
        page = doc[0]
        width = page.get_width() or COVER_WIDTH
        scale = COVER_WIDTH / width
        image = page.render(scale=scale).to_pil().convert("RGB")
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue()
    except Exception:
        logger.exception("Не удалось отрисовать первую страницу PDF")
        return None
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


def build_cover_key(base_name: str) -> str:
    slug = slugify(base_name) or "cover"
    return f"cover/{slug}-{int(time.time() * 1000)}.jpg"


def generate_cover_from_pdf(pdf_url: str, base_name: str) -> str | None:
    """Скачать PDF из R2, отрисовать обложку, загрузить обратно.

    Возвращает публичный URL обложки или None. Сознательно не бросает: вызов
    идёт фоном после сохранения статьи, и сбой рендера не должен ничего ломать.
    """
    key = key_from_url(pdf_url, default_prefix="pdfs")
    if not key:
        return None

    try:
        # Первая страница лежит в начале файла далеко не всегда, поэтому берём
        # объект целиком — это разовая операция на сервере, а не на каждый показ.
        pdf_bytes, _ = storage.get(key)
    except StorageNotConfigured:
        logger.warning("R2 не настроен — обложка не создана для %s", key)
        return None
    except KeyError:
        logger.warning("PDF не найден в хранилище: %s", key)
        return None
    except Exception:
        logger.exception("Не удалось прочитать PDF %s", key)
        return None

    jpeg = render_first_page(pdf_bytes)
    if not jpeg:
        return None

    cover_key = build_cover_key(base_name)
    try:
        storage.put(cover_key, jpeg, "image/jpeg")
    except Exception:
        logger.exception("Не удалось загрузить обложку %s", cover_key)
        return None

    return public_url(cover_key) or cover_key

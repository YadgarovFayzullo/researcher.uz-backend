"""Допуск к OAI-PMH: реестр ключей вместо открытого эндпоинта.

Зачем закрыто. OAI-PMH создавался как «забирай всё»: `ListRecords` отдаёт базу
целиком, страницами, без единого вопроса. Для нас это значит, что открытый /oai
— готовая кнопка «склонировать каталог researcher.uz». Партнёрам (EBSCO, BASE,
DOAJ) выдача нужна, всем остальным — нет, поэтому ключ обязателен: без него 401,
и никакой информации о наличии данных наружу не уходит.

Ключ приходит одним из трёх способов:
  * `?key=<token>` — основной. Харвестеру обычно можно настроить только baseURL,
    заголовки он ставить не умеет. Ключ при этом попадает в access-логи Caddy —
    отсюда правило: ключ на клиента, отзывается одной командой;
  * `Authorization: Bearer <token>` — для ручной проверки и наших скриптов;
  * `X-OAI-Key: <token>`.

Хранится только sha256 ключа: утечка дампа БД не даёт доступа. Выдача и отзыв —
scripts/oai_client.py.
"""
from __future__ import annotations

import hashlib
import ipaddress
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.persistence.models import OaiClient

TOKEN_QUERY_PARAM = "key"
TOKEN_HEADER = "X-OAI-Key"


def new_token() -> str:
    """Ключ для нового клиента. 32 байта энтропии — перебор невозможен."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def extract_token(request: Request) -> str | None:
    header = request.headers.get("Authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip() or None
    direct = request.headers.get(TOKEN_HEADER)
    if direct:
        return direct.strip() or None
    param = request.query_params.get(TOKEN_QUERY_PARAM)
    return param.strip() if param and param.strip() else None


def client_ip(request: Request) -> str | None:
    """IP клиента. XFF уже разобран uvicorn (--proxy-headers, см. entrypoint)."""
    return request.client.host if request.client else None


def ip_allowed(ip: str | None, allowlist: list[str] | None) -> bool:
    """Пустой список = ограничения нет. Иначе IP должен попасть в сеть из списка.

    Битую запись в списке молча пропускаем — иначе опечатка в одном CIDR
    закрыла бы доступ целиком и выглядела бы как отзыв ключа.
    """
    if not allowlist:
        return True
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowlist:
        try:
            if addr in ipaddress.ip_network(entry.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def _denied(detail: str) -> HTTPException:
    """401 на всё: и «ключа нет», и «ключ отозван», и «не тот IP».

    Один и тот же ответ намеренно — чтобы по коду ошибки нельзя было
    установить, существует ли подобранный ключ.
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": 'Bearer realm="OAI-PMH"'},
    )


async def authenticate(request: Request, db: AsyncSession) -> OaiClient:
    """Клиент по ключу из запроса. 401, если ключа нет или он недействителен."""
    generic = (
        "OAI-PMH access requires a harvester key issued by researcher.uz "
        "(info@researcher.uz)."
    )
    token = extract_token(request)
    if not token:
        raise _denied(generic)

    res = await db.execute(
        select(OaiClient).where(OaiClient.token_hash == hash_token(token))
    )
    client = res.scalar_one_or_none()
    if client is None or not client.enabled:
        raise _denied(generic)

    now = datetime.now(timezone.utc)
    if client.expires_at is not None and client.expires_at <= now:
        raise _denied(generic)
    if not ip_allowed(client_ip(request), client.ip_allowlist):
        raise _denied(generic)

    await _touch(db, client, now, client_ip(request))
    return client


async def _touch(
    db: AsyncSession, client: OaiClient, now: datetime, ip: str | None
) -> None:
    """Отметка обращения — чтобы в любой момент было видно, кто и когда качал.

    Пишем через UPDATE ... requests_count + 1, а не чтением-записью: воркеров
    несколько, и параллельные харвесты не должны терять счёт.
    """
    await db.execute(
        update(OaiClient)
        .where(OaiClient.id == client.id)
        .values(
            last_seen_at=now,
            last_seen_ip=ip,
            requests_count=OaiClient.requests_count + 1,
        )
    )
    await db.commit()

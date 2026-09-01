"""OAI-PMH: выборка записей и раскладка их в Dublin Core.

Что отдаём наружу. Только опубликованное (`articles.published`), со слагом (без
него нет адреса записи) и не из демо-журнала — тот же фильтр, что у витрины,
иначе клиентский стенд уехал бы в мировые базы вместе с настоящим каталогом.

Наборы (sets):
  * `journal:<slug>` — статьи журнала/сборника конференции;
  * `type:<publication_type>` — статьи, монографии, диссертации и т.д.
Клиенту, у которого в `allowed_journal_ids` есть ограничение, видны только его
журналы — и в ListSets, и в любой выдаче, даже если он подставит чужой set
руками или в resumptionToken.

Пагинация — keyset по (updated_at, id), а не OFFSET: харвест базы идёт часами,
и за это время правка статьи сдвинула бы окно, а с ним и границы страниц.
Курсор и фильтры лежат в resumptionToken, но права клиента при каждом запросе
применяются заново — токен не может расширить доступ.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import Select, and_, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.domain.demo import article_is_not_demo, journal_is_not_demo
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    Issue,
    Journal,
    Publisher,
)

GRANULARITY = "YYYY-MM-DDThh:mm:ssZ"


class OaiError(Exception):
    """Ошибка протокола: code — из списка OAI-PMH (badArgument и т.п.)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


# ------------------------------------------------------------------ даты

def format_datestamp(value: datetime | None) -> str:
    if value is None:
        return "1970-01-01T00:00:00Z"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_datestamp(raw: str, *, end_of_day: bool) -> datetime:
    """`from`/`until` из запроса. Допускаем обе гранулярности спецификации.

    Для даты без времени `until` означает конец суток включительно — иначе
    харвестер, спросивший until=2026-09-01, не получил бы ничего за этот день.
    """
    text = raw.strip()
    try:
        if text.endswith("Z"):
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
            return parsed.replace(tzinfo=timezone.utc)
        day = date.fromisoformat(text)
    except ValueError:
        raise OaiError("badArgument", f"Malformed date: {raw}")
    edge = time(23, 59, 59) if end_of_day else time(0, 0, 0)
    return datetime.combine(day, edge, tzinfo=timezone.utc)


# ------------------------------------------------------------------ права

@dataclass
class Scope:
    """Что этому ключу разрешено видеть."""

    journal_ids: list[int] = field(default_factory=list)
    include_fulltext: bool = False

    @classmethod
    def from_client(cls, client) -> "Scope":
        return cls(
            journal_ids=list(client.allowed_journal_ids or []),
            include_fulltext=bool(client.include_fulltext),
        )


@dataclass
class Selector:
    """Фильтры запроса: окно по датам и набор."""

    frm: datetime | None = None
    until: datetime | None = None
    set_spec: str | None = None


# ------------------------------------------------------------------ токены

def _iso(value: datetime | None) -> str | None:
    """Время внутрь токена — с микросекундами.

    Гранулярность OAI (`format_datestamp`) обрезает до секунды, и курсор,
    записанный так, отъезжал бы назад: все записи той же секунды с ненулевыми
    микросекундами приходили бы повторно на каждой странице. Токен читаем только
    мы, так что здесь полная точность.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _from_iso(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def encode_token(
    selector: Selector, cursor: tuple[datetime, int], total: int, prefix: str
) -> str:
    payload = {
        "f": _iso(selector.frm),
        "u": _iso(selector.until),
        "s": selector.set_spec,
        "p": prefix,
        "c": [_iso(cursor[0]), cursor[1]],
        "n": total,
        "e": _iso(
            datetime.now(timezone.utc)
            + timedelta(seconds=settings.OAI_TOKEN_TTL_SECONDS)
        ),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_token(token: str) -> tuple[Selector, tuple[datetime, int], int, str]:
    """Токен → (фильтры, курсор, всего, metadataPrefix).

    Подпись не нужна: всё, что можно подделать в токене, — фильтры выдачи, а
    права клиента накладываются поверх них отдельно. Протухший токен = ошибка
    badResumptionToken, харвестер начнёт обход заново (штатное поведение).
    """
    try:
        pad = "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(token + pad))
        expires = _from_iso(payload["e"])
        cursor = (_from_iso(payload["c"][0]), int(payload["c"][1]))
        selector = Selector(
            frm=_from_iso(payload["f"]) if payload.get("f") else None,
            until=_from_iso(payload["u"]) if payload.get("u") else None,
            set_spec=payload.get("s"),
        )
        prefix = payload.get("p") or "oai_dc"
        total = int(payload.get("n") or 0)
    except (ValueError, KeyError, TypeError, IndexError):
        raise OaiError("badResumptionToken", "Malformed resumptionToken")
    if expires <= datetime.now(timezone.utc):
        raise OaiError("badResumptionToken", "resumptionToken has expired")
    return selector, cursor, total, prefix


# ------------------------------------------------------------------ выборка

class OaiDomain:
    """Запросы к каталогу для OAI-выдачи."""

    @staticmethod
    def _visible(scope: Scope):
        """Условия «эту запись вообще можно отдать наружу»."""
        conds = [
            Article.published.is_(True),
            Article.slug.isnot(None),
            Article.slug != "",
            Article.title.isnot(None),
            article_is_not_demo(),
        ]
        if scope.journal_ids:
            # Ограниченному клиенту самостоятельные издания (issue_id IS NULL)
            # не видны: они не принадлежат ни одному разрешённому журналу.
            conds.append(Issue.journal_id.in_(scope.journal_ids))
        return conds

    @classmethod
    def _query(cls, scope: Scope, selector: Selector) -> Select:
        q = (
            select(Article, Issue, Journal, Publisher)
            .select_from(Article)
            .outerjoin(Issue, Article.issue_id == Issue.id)
            .outerjoin(Journal, Issue.journal_id == Journal.id)
            .outerjoin(Publisher, Article.publisher_id == Publisher.id)
            .where(*cls._visible(scope))
        )
        if selector.frm is not None:
            q = q.where(Article.updated_at >= selector.frm)
        if selector.until is not None:
            q = q.where(Article.updated_at <= selector.until)
        if selector.set_spec:
            q = q.where(cls._set_condition(selector.set_spec))
        return q

    @staticmethod
    def _set_condition(set_spec: str):
        kind, _, value = set_spec.partition(":")
        if kind == "journal" and value:
            return and_(Journal.slug == value, journal_is_not_demo())
        if kind == "type" and value:
            return Article.publication_type == value
        raise OaiError("noRecordsMatch", f"Unknown set: {set_spec}")

    @classmethod
    async def count(cls, db: AsyncSession, scope: Scope, selector: Selector) -> int:
        q = cls._query(scope, selector).with_only_columns(func.count(Article.id))
        return int((await db.execute(q.order_by(None))).scalar() or 0)

    @classmethod
    async def page(
        cls,
        db: AsyncSession,
        scope: Scope,
        selector: Selector,
        cursor: tuple[datetime, int] | None,
        limit: int,
    ) -> list:
        q = cls._query(scope, selector)
        if cursor is not None:
            # Keyset: строго «после» последней отданной записи.
            q = q.where(tuple_(Article.updated_at, Article.id) > tuple_(*cursor))
        q = q.order_by(Article.updated_at.asc(), Article.id.asc()).limit(limit)
        return list((await db.execute(q)).all())

    @classmethod
    async def get_one(cls, db: AsyncSession, scope: Scope, article_id: int):
        q = cls._query(scope, Selector()).where(Article.id == article_id)
        return (await db.execute(q)).first()

    @classmethod
    async def earliest(cls, db: AsyncSession, scope: Scope) -> datetime | None:
        q = cls._query(scope, Selector()).with_only_columns(
            func.min(Article.updated_at)
        )
        return (await db.execute(q.order_by(None))).scalar()

    @classmethod
    async def sets(cls, db: AsyncSession, scope: Scope) -> list[tuple[str, str]]:
        """(setSpec, setName): журналы, доступные ключу, плюс типы публикаций."""
        q = (
            select(Journal.slug, Journal.name)
            .where(Journal.slug.isnot(None), Journal.slug != "", journal_is_not_demo())
            .order_by(Journal.name)
        )
        if scope.journal_ids:
            q = q.where(Journal.id.in_(scope.journal_ids))
        rows = (await db.execute(q)).all()
        result = [(f"journal:{slug}", name or slug) for slug, name in rows]

        types_q = (
            select(Article.publication_type)
            .select_from(Article)
            .outerjoin(Issue, Article.issue_id == Issue.id)
            .where(*cls._visible(scope))
            .group_by(Article.publication_type)
            .order_by(Article.publication_type)
        )
        for (ptype,) in (await db.execute(types_q)).all():
            if ptype:
                result.append((f"type:{ptype}", ptype))
        return result

    @staticmethod
    async def authors(db: AsyncSession, article_ids: list[int]) -> dict[int, list[str]]:
        """Авторы страницы одним запросом — иначе N+1 на каждую запись."""
        if not article_ids:
            return {}
        rows = (
            await db.execute(
                select(ArticleAuthor.article_id, ArticleAuthor.author_name)
                .where(ArticleAuthor.article_id.in_(article_ids))
                .order_by(ArticleAuthor.article_id, ArticleAuthor.author_order)
            )
        ).all()
        out: dict[int, list[str]] = {}
        for article_id, name in rows:
            if name:
                out.setdefault(int(article_id), []).append(name)
        return out

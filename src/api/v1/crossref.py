"""Регистрация DOI в Crossref (`/crossref`).

Доступ — владелец платформы. Как импорт и антиплагиат, это платная услуга: за
каждый депонированный DOI издатель платит Crossref, а сам депозит необратим —
DOI можно обновить, но не отозвать. Отдавать такую кнопку редактору журнала до
появления тарификации и лимитов нельзя.

Порядок работы намеренно двухшаговый: сначала `preview` — что именно уедет
(включая эвристический разбор ФИО), и только потом `deposit`. Результат
приходит асинхронно, поэтому `deposit` возвращает «отправлено», а не
«зарегистрировано»; настоящий вердикт забирает `refresh`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import require_owner
from src.core.config import settings
from src.domain.crossref import (
    CrossrefDomain,
    CrossrefError,
    build_batch_id,
    build_deposit_xml,
    parse_pages,
    resolve_credentials,
    resource_url,
)
from src.infrastructure.external.crossref import (
    CrossrefTransportError,
    fetch_result,
    submit_batch,
)
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Article, CrossrefDeposit, Profile
from src.schemas.crossref import (
    CrossrefContributor,
    CrossrefDepositPublic,
    CrossrefPreview,
)

router = APIRouter()
domain = CrossrefDomain()


def _bad_request(exc: CrossrefError) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.get("/status")
async def deposit_status(_owner: Profile = Depends(require_owner)):
    """Настроен ли депозит и в какой среде. Интерфейс обязан показывать среду:
    прогон в песочнице и боевая регистрация выглядят одинаково, а стоят
    по-разному."""
    return {
        "enabled": settings.CROSSREF_ENABLED,
        "environment": settings.CROSSREF_ENV,
        "base_url": settings.CROSSREF_BASE_URL,
        "default_prefix": settings.CROSSREF_DEFAULT_PREFIX,
        "accounts": sorted(settings.crossref_accounts.keys()),
        "suffix_template": settings.CROSSREF_DOI_SUFFIX_TEMPLATE,
    }


@router.get("/articles/{article_id}/preview", response_model=CrossrefPreview)
async def preview(
    article_id: int,
    with_xml: bool = Query(False, description="Вернуть готовый XML депозита"),
    _owner: Profile = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Что уедет в Crossref. Смотреть до отправки — депозит необратим."""
    try:
        item = await domain.load_item(db, article_id)
    except CrossrefError as exc:
        raise _bad_request(exc)

    deposit = await domain.get_deposit(db, article_id)
    first_page, last_page = parse_pages(item.article.pages)
    xml = None
    if with_xml:
        stamp = datetime.now(timezone.utc)
        xml = build_deposit_xml(
            item, batch_id=build_batch_id(item.article.id, stamp), stamp=stamp
        ).decode("utf-8")

    return CrossrefPreview(
        article_id=item.article.id,
        doi=deposit.doi if deposit else item.doi,
        environment=settings.CROSSREF_ENV,
        resource_url=resource_url(item.article),
        journal_name=item.journal.name,
        issn=item.journal.issn,
        printed_issn=item.journal.printed_issn,
        volume=item.issue.volume,
        issue=item.issue.issue,
        title=item.article.title,
        publication_date=item.publication_date,
        first_page=first_page,
        last_page=last_page,
        contributors=[
            CrossrefContributor(
                given_name=c.given_name, surname=c.surname, orcid=c.orcid
            )
            for c in item.contributors
        ],
        citations_count=len(item.citations),
        citations_with_doi=sum(1 for c in item.citations if c.doi),
        problems=item.problems(),
        deposit=CrossrefDepositPublic.model_validate(deposit) if deposit else None,
        xml=xml,
    )


@router.post("/articles/{article_id}/deposit", response_model=CrossrefDepositPublic)
async def deposit_article(
    article_id: int,
    force: bool = Query(
        False,
        description="Перезалить метаданные уже зарегистрированного DOI",
    ),
    owner: Profile = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    try:
        item = await domain.load_item(db, article_id)
        creds = resolve_credentials(item.journal)
    except CrossrefError as exc:
        raise _bad_request(exc)

    problems = item.problems()
    if problems:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"code": "incomplete_metadata", "problems": problems},
        )

    deposit = await domain.upsert_deposit(db, item=item, created_by=owner.id)
    if deposit.status == "registered" and not force:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"DOI {deposit.doi} уже зарегистрирован; для обновления метаданных "
            "нужен force=true",
        )
    # DOI берём со строки депозита, а не из свежего расчёта: у зарегистрированной
    # статьи он неизменен, даже если шаблон суффикса с тех пор поменяли.
    item.doi = deposit.doi

    stamp = datetime.now(timezone.utc)
    batch_id = build_batch_id(item.article.id, stamp)
    xml = build_deposit_xml(item, batch_id=batch_id, stamp=stamp)

    deposit.attempts = (deposit.attempts or 0) + 1
    try:
        response = await submit_batch(xml, batch_id=batch_id, creds=creds)
    except CrossrefTransportError as exc:
        deposit.status = "failed"
        deposit.error = str(exc)[:2000]
        await db.commit()
        await db.refresh(deposit)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))

    deposit.batch_id = batch_id
    deposit.status = "submitted"
    deposit.error = None
    deposit.submitted_at = stamp
    deposit.result = {"submit_response": response[:2000], "account": creds.account}
    await db.commit()
    await db.refresh(deposit)
    return deposit


@router.post("/deposits/{deposit_id}/refresh", response_model=CrossrefDepositPublic)
async def refresh_deposit(
    deposit_id: int,
    _owner: Profile = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Забрать вердикт Crossref по отправленному батчу.

    Пока батч не разобран, статус остаётся `submitted` — это не ошибка и не
    повод отправлять его заново.
    """
    res = await db.execute(
        select(CrossrefDeposit).where(CrossrefDeposit.id == deposit_id)
    )
    deposit = res.scalar_one_or_none()
    if deposit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Депозит не найден")
    if not deposit.batch_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Депозит ещё не отправлялся"
        )

    try:
        item = await domain.load_item(db, deposit.article_id)
        creds = resolve_credentials(item.journal)
    except CrossrefError as exc:
        raise _bad_request(exc)

    try:
        result = await fetch_result(deposit.batch_id, creds=creds)
    except CrossrefTransportError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))

    deposit.checked_at = datetime.now(timezone.utc)
    deposit.result = {
        **(deposit.result or {}),
        "status": result.status,
        "success": result.success,
        "failure": result.failure,
        "messages": result.messages,
    }

    if result.is_pending:
        await db.commit()
        await db.refresh(deposit)
        return deposit

    if result.is_success:
        deposit.status = "registered"
        deposit.registered_at = deposit.checked_at
        deposit.error = None
        # DOI в саму статью пишем только из боевой среды: песочница ничего не
        # регистрирует, и её DOI на странице статьи вёл бы в никуда.
        if deposit.environment == "production" and item.article.doi != deposit.doi:
            await db.execute(
                Article.__table__.update()
                .where(Article.id == deposit.article_id)
                .values(doi=deposit.doi)
            )
    else:
        deposit.status = "failed"
        deposit.error = "; ".join(result.messages)[:2000] or (
            f"Crossref вернул статус {result.status}"
        )

    await db.commit()
    await db.refresh(deposit)
    return deposit


@router.get("/deposits", response_model=list[CrossrefDepositPublic])
async def list_deposits(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _owner: Profile = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    query = select(CrossrefDeposit).order_by(CrossrefDeposit.created_at.desc())
    if status_filter:
        query = query.where(CrossrefDeposit.status == status_filter)
    res = await db.execute(query.limit(limit).offset(offset))
    return list(res.scalars().all())

import logging

from fastapi import BackgroundTasks, Depends, APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field

from src.api.deps import get_current_profile, require_owner
from src.core.config import settings
from src.domain.authz import can_write_article
from src.domain import ai_review, moderation
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Article, Profile
from src.schemas.article import ArticleCreate, ArticleUpdate
from src.domain.authors import AuthorCardDomain
from src.domain.article import ArticleDomain
from src.domain.serialization import HEAVY_ARTICLE_COLUMNS, row_to_dict
from src.infrastructure.covers import generate_cover_from_pdf
from src.infrastructure.persistence.db import AsyncSessionLocal

router = APIRouter()
domain = ArticleDomain()
_author_cards = AuthorCardDomain()

logger = logging.getLogger(__name__)


async def _fill_cover(article_id: int) -> None:
    """Догенерировать обложку из PDF, если её не задали руками.

    Работает фоном: рендер занимает секунды, а ответ формы ждать их не должен.
    Своя сессия — та, что обслуживала запрос, к этому моменту уже закрыта.
    """
    async with AsyncSessionLocal() as db:
        article = await domain.get_article_by_id(db, article_id)
        if not article or article.cover_image or not article.pdf:
            return
        url = await run_in_threadpool(
            generate_cover_from_pdf, article.pdf, article.title or article.slug or "cover"
        )
        if not url:
            return
        # Перечитываем перед записью: за время рендера обложку могли выставить
        # вручную, и затирать её результатом фона нельзя.
        fresh = await domain.get_article_by_id(db, article_id)
        if not fresh or fresh.cover_image:
            return
        fresh.cover_image = url
        await db.commit()
        logger.info("Обложка сгенерирована для статьи %s", article_id)


def _forbidden() -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed to write this article")


def _public(article: Article) -> dict:
    """Строка статьи без служебных колонок поиска.

    ORM-объект, отданный FastAPI напрямую, сериализуется целиком — вместе с
    768-мерным `embedding` и тремя tsvector'ами. В ответе `/article/<slug>` это
    16 КБ из 18 КБ, и каждый из них уезжал в HTML страницы статьи (RSC-пейлоад),
    а оттуда — в трафик compute → CDN на каждом ISR-рендере. Список статей
    вычищал их давно (`_LIST_EXCLUDE`), одиночная статья — нет.
    """
    data = row_to_dict(article, Article, exclude=HEAVY_ARTICLE_COLUMNS)
    # JSONB-колонка в модели названа `meta` (слово `metadata` занято SQLAlchemy),
    # и голая сериализация строки отдавала наружу именно её. Списки же идут через
    # `ArticleListResponse`, где поле называется `metadata` — и фронт с
    # генерированными типами ждёт того же. Из-за расхождения `article.metadata`
    # на странице статьи всегда был undefined. Приводим к общему имени.
    data["metadata"] = data.pop("meta", None)
    return data


@router.get("/resolve/{slug}")
async def resolve_slug(slug: str, db: AsyncSession = Depends(get_db)):
    """Актуальный слаг для старого адреса — для 301 со страницы статьи.

    Двухсегментный путь с `/{slug}` не конфликтует: тот матчит один сегмент.
    """
    target = await domain.resolve_legacy_slug(db, slug)
    if not target:
        raise HTTPException(status_code=404, detail="No unambiguous match")
    return {"slug": target}


@router.get("/{slug}")
async def get_article(slug: str, db: AsyncSession = Depends(get_db)):
    # published_only: снятая с публикации статья на сайте не открывается.
    # Админка грузит её по id (/articles/by-id/{id}), поэтому редактирование
    # снятой статьи по-прежнему работает.
    article = await domain.get_article_by_slug(db, slug, published_only=True)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    # Слаги карточек авторов: страница статьи ссылается на них именами авторов.
    # Раньше имя вело в /search, а он noindex — перелинковки для робота не было.
    cards = await _author_cards.cards_for_article(db, article.id)
    return {"status": "ok", "article": _public(article), "author_cards": cards}


@router.post("/", status_code=201)
async def create_article(
    article_in: ArticleCreate,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    # Авторизация по полям создаваемой строки (rls_content.sql: articles insert).
    allowed = await can_write_article(
        db,
        role=profile.role,
        user_id=profile.id,
        issue_id=article_in.issue_id,
        admin_id=getattr(article_in, "admin_id", None),
        publisher_id=getattr(article_in, "publisher_id", None),
    )
    if not allowed:
        raise _forbidden()
    article = await domain.create_article(db, article_in)

    # Статья в выпуске не публикуется сразу: сначала ИИ сверяет метаданные с
    # приложенным PDF (см. src/domain/ai_review.py). Через час после последней
    # залитой в этот выпуск статьи планировщик проверит их пачкой и либо
    # опубликует, либо погасит выпуск и напишет владельцу. Отдельные издания
    # (монографии, диссертации) сюда не попадают — у них нет выпуска.
    queued = False
    if settings.REVIEW_ACTIVE and article.issue_id:
        article.published = False
        ai_review.queue_article(article)
        await db.commit()
        await db.refresh(article)
        queued = True

    if article.pdf and not article.cover_image:
        background.add_task(_fill_cover, article.id)
    return {
        "status": "created",
        "slug": article.slug,
        "article": _public(article),
        "ai_review_queued": queued,
    }


@router.patch("/{id}")
async def update_article(
    id: int,
    article_in: ArticleUpdate,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    existing = await domain.get_article_by_id(db, id)
    if not existing:
        raise HTTPException(status_code=404, detail="Article not found")
    # Право на существующую строку (RLS `using`). ArticleUpdate не меняет
    # issue_id/admin_id/publisher_id, поэтому `with check` == `using`.
    allowed = await can_write_article(
        db,
        role=profile.role,
        user_id=profile.id,
        issue_id=existing.issue_id,
        admin_id=existing.admin_id,
        publisher_id=existing.publisher_id,
    )
    if not allowed:
        raise _forbidden()
    article = await domain.update_article(db, id, article_in)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    if article.pdf and not article.cover_image:
        background.add_task(_fill_cover, article.id)
    return {"status": "updated", "article": _public(article)}


@router.delete("/{id}")
async def delete_article(
    id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    existing = await domain.get_article_by_id(db, id)
    if not existing:
        raise HTTPException(status_code=404, detail="Article not found")
    allowed = await can_write_article(
        db,
        role=profile.role,
        user_id=profile.id,
        issue_id=existing.issue_id,
        admin_id=existing.admin_id,
        publisher_id=existing.publisher_id,
    )
    if not allowed:
        raise _forbidden()
    # Слаг читаем до удаления: после него строки уже нет, а фронту он нужен —
    # иначе страница удалённой статьи продолжит отдаваться из ISR-кэша неделю.
    slug = existing.slug
    deleted = await domain.delete_articles(db, [id], deleted_by=profile.id) > 0
    if not deleted:
        raise HTTPException(status_code=404, detail="Article not found")
    return {"status": "deleted", "revalidate_slugs": [slug] if slug else []}


class BulkDeleteRequest(BaseModel):
    # Потолок совпадает с MAX_SLUGS у /api/revalidate фронта: больше слагов
    # сброс кэша не примет, и хвост удалённых висел бы на сайте из ISR.
    ids: list[int] = Field(min_length=1, max_length=200)


@router.post("/bulk-delete")
async def bulk_delete_articles(
    body: BulkDeleteRequest,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(get_current_profile),
):
    """Удалить несколько статей разом — массовое удаление в админке выпуска.

    Всё или ничего: если хоть на одну статью прав нет, не удаляется ни одна.
    Иначе редактор получил бы наполовину выполненную операцию и не понял бы,
    что осталось. Уже удалённые кем-то id молча пропускаем.
    """
    rows = (
        await db.execute(select(Article).where(Article.id.in_(set(body.ids))))
    ).scalars().all()
    for row in rows:
        allowed = await can_write_article(
            db,
            role=profile.role,
            user_id=profile.id,
            issue_id=row.issue_id,
            admin_id=row.admin_id,
            publisher_id=row.publisher_id,
        )
        if not allowed:
            raise _forbidden()
    slugs = [row.slug for row in rows if row.slug]
    deleted = await domain.delete_articles(
        db, [row.id for row in rows], deleted_by=profile.id
    )
    return {"status": "deleted", "deleted": deleted, "revalidate_slugs": slugs}


class TakedownRequest(BaseModel):
    """Причина обязательна: снятие гасит весь выпуск, и через полгода никто не
    вспомнит, за что именно, если не записать."""

    reason: str = Field(min_length=3, max_length=500)


@router.post("/{id}/takedown")
async def takedown_article(
    id: int,
    body: TakedownRequest,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(require_owner),
):
    """Снять статью с публикации как нарушение и погасить её выпуск.

    Только владелец: это санкция против редактора журнала, и сам редактор
    отменить её не должен.
    """
    article = await domain.get_article_by_id(db, id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    result = await moderation.takedown_article(
        db, article, reason=body.reason, by=profile.id
    )
    # Слаги всего, что погасла эта санкция (нарушитель + статьи его выпуска), —
    # админка сбрасывает по ним ISR-кэш страниц. Без этого снятое остаётся
    # открытым на сайте до конца срока кэша, то есть до месяца.
    blocked = result.get("issue_blocked") or {}
    result["revalidate_slugs"] = await moderation.article_slugs(
        db, [article.id, *(blocked.get("article_ids") or [])]
    )
    return {"status": "taken_down", **result}


@router.post("/{id}/restore")
async def restore_article(
    id: int,
    db: AsyncSession = Depends(get_db),
    profile: Profile = Depends(require_owner),
):
    """Убрать отметку нарушения со статьи. Публикацию и блокировку выпуска не
    трогает — статья возвращается в ленты обычным сохранением формы, выпуск
    открывается через `/issues/{id}/unblock`."""
    article = await domain.get_article_by_id(db, id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    await moderation.restore_article(db, article)
    return {
        "status": "restored",
        "article_id": id,
        "revalidate_slugs": [article.slug] if article.slug else [],
    }

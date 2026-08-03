import logging

from fastapi import BackgroundTasks, Depends, APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.deps import get_current_profile
from src.domain.authz import can_write_article
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import Profile
from src.schemas.article import ArticleCreate, ArticleUpdate
from src.domain.article import ArticleDomain
from src.infrastructure.covers import generate_cover_from_pdf
from src.infrastructure.persistence.db import AsyncSessionLocal

router = APIRouter()
domain = ArticleDomain()

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


@router.get("/{slug}")
async def get_article(slug: str, db: AsyncSession = Depends(get_db)):
    article = await domain.get_article_by_slug(db, slug)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return {"status": "ok", "article": article}


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
    if article.pdf and not article.cover_image:
        background.add_task(_fill_cover, article.id)
    return {"status": "created", "slug": article.slug, "article": article}


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
    return {"status": "updated", "article": article}


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
    deleted = await domain.delete_article(db, id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Article not found")
    return {"status": "deleted"}

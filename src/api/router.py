from fastapi import APIRouter
from src.api.v1 import (
    journals,
    journal,
    issues,
    publishers,
    sections,
    articles,
    article_content,
    library,
    stats,
    article,
    auth,
    google_auth,
    orcid_auth,
    admin,
    citations,
    researcher,
    files,
    uploads,
    search,
    news,
    security,
    imports,
)

api_router = APIRouter()

api_router.include_router(journals.router, prefix="/journals", tags=["journals"])
api_router.include_router(journal.router, prefix="/journal", tags=["journal"])
api_router.include_router(issues.router, prefix="/issues", tags=["issues"])
api_router.include_router(publishers.router, prefix="/publishers", tags=["publishers"])
api_router.include_router(
    sections.router, prefix="/conference-sections", tags=["conference-sections"]
)
api_router.include_router(articles.router, prefix="/articles", tags=["articles"])
# Под-ресурсы статьи: /articles/{id}/authors, /articles/{id}/references.
api_router.include_router(
    article_content.router, prefix="/articles", tags=["article-content"]
)
api_router.include_router(library.router, prefix="/library", tags=["library"])
api_router.include_router(stats.router, prefix="/stats", tags=["stats"])
api_router.include_router(article.router, prefix="/article", tags=["article"])

# Auth (Фаза 3)
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(google_auth.router, prefix="/auth/google", tags=["auth"])
api_router.include_router(orcid_auth.router, prefix="/auth/orcid", tags=["auth"])

# Owner-консоль (Фаза 4)
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])

# RPC → сервисы (Фаза 5)
api_router.include_router(citations.router, prefix="/citations", tags=["citations"])
api_router.include_router(researcher.router, prefix="/researcher", tags=["researcher"])

# Storage / R2 (Фаза 6)
api_router.include_router(files.router, tags=["files"])  # /pdf/<filename>
api_router.include_router(uploads.router, prefix="/storage", tags=["storage"])

# Поиск (Фаза 7)
api_router.include_router(search.router, prefix="/search", tags=["search"])

# Новости платформы
api_router.include_router(news.router, prefix="/news", tags=["news"])

# Антибот: обмен токена Turnstile на cookie-пропуск
api_router.include_router(security.router, prefix="/security", tags=["security"])
# Импорт архивов с других платформ (import-integration.md).
api_router.include_router(imports.router, prefix="/import", tags=["import"])

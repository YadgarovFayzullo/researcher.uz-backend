from fastapi import APIRouter
from src.api.v1 import (
    authors,
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
    article_trash,
    auth,
    google_auth,
    orcid_auth,
    admin,
    citations,
    researcher,
    files,
    uploads,
    zenodo,
    search,
    news,
    security,
    imports,
    folder_import,
    plagiarism,
    oai,
    crossref,
    telegram,
    outreach,
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
api_router.include_router(authors.router, prefix="/authors", tags=["authors"])
# Под-ресурсы статьи: /articles/{id}/authors, /articles/{id}/references.
api_router.include_router(
    article_content.router, prefix="/articles", tags=["article-content"]
)
api_router.include_router(library.router, prefix="/library", tags=["library"])
api_router.include_router(stats.router, prefix="/stats", tags=["stats"])
api_router.include_router(article.router, prefix="/article", tags=["article"])
# Корзина удалённых статей: 30 дней на восстановление, owner-only.
api_router.include_router(
    article_trash.router, prefix="/article-trash", tags=["article-trash"]
)

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
# Депонирование в Zenodo — токен на сервере, форма шлёт файл сюда
api_router.include_router(zenodo.router, prefix="/zenodo", tags=["zenodo"])

# Поиск (Фаза 7)
api_router.include_router(search.router, prefix="/search", tags=["search"])

# Новости платформы
api_router.include_router(news.router, prefix="/news", tags=["news"])

# Антибот: обмен токена Turnstile на cookie-пропуск
api_router.include_router(security.router, prefix="/security", tags=["security"])
# Импорт архивов с других платформ (import-integration.md).
api_router.include_router(imports.router, prefix="/import", tags=["import"])
# Загрузка папки PDF редактором: метаданные берутся из самих файлов.
# В отличие от импорта архивов — не owner-only, права считаются по выпуску.
api_router.include_router(
    folder_import.router, prefix="/import/folder", tags=["import"]
)
# Проверка на заимствования по базе платформы.
api_router.include_router(plagiarism.router, prefix="/plagiarism", tags=["plagiarism"])
# Вебхук Telegram: кнопки «опубликовать / оставить закрытым» под разбором
# ИИ-проверки выпуска (ai-review-integration.md). Авторизации у Telegram нет,
# ручка закрыта секретом вебхука и chat_id владельца.
api_router.include_router(telegram.router, prefix="/telegram", tags=["telegram"])

# OAI-PMH: выдача метаданных партнёрам-агрегаторам. Не публичный эндпоинт —
# закрыт ключом из oai_clients (src/core/oai_access.py), поэтому и prefix
# короткий: baseURL партнёру даётся как https://api.researcher.uz/oai.
api_router.include_router(oai.router, prefix="/oai", tags=["oai"])

# Регистрация DOI в Crossref (crossref-integration.md). Owner-only: депозит
# стоит денег издателю и необратим.
api_router.include_router(crossref.router, prefix="/crossref", tags=["crossref"])

# Отписка от рассылки авторам. Публичная и без сессии: человек приходит из
# письма, аккаунта у него нет. Адрес защищён HMAC-подписью в ссылке.
api_router.include_router(outreach.router, prefix="/outreach", tags=["outreach"])

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler
from src.api.router import api_router
from src.core.config import settings
from src.core.ratelimit import limiter
from src.infrastructure import scheduler, trash_purge
from src.infrastructure.persistence.db import DatabaseUnavailableError


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Планировщик ИИ-проверки выпусков. Без ANTHROPIC_API_KEY не стартует, так
    # что на машине без ключа поведение приложения ровно прежнее.
    scheduler.start(app)
    # Чистка корзины статей (30 дней) — всегда, независимо от ключей.
    trash_purge.start(app)
    try:
        yield
    finally:
        await trash_purge.stop(app)
        await scheduler.stop(app)


app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)

# Rate-limiting (slowapi). Лимитер в app.state + обработчик 429. Отдельные
# эндпоинты (auth) декорируются @limiter.limit в своих роутерах.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Сессия живёт в httpOnly-cookie, поэтому allow_credentials=True и явный
# список origin'ов (со звёздочкой браузер cookie не пошлёт). Фронт и API
# на разных хостах — без этого не пройдёт ни один браузерный запрос.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.exception_handler(DatabaseUnavailableError)
async def database_unavailable_handler(request: Request, exc: DatabaseUnavailableError):
    return JSONResponse(
        status_code=503,
        content={"detail": "Database is temporarily unavailable. Please try again later."},
    )


@app.get("/")
async def root():
    return {"message": "Welcome to Scientific Backend API"}
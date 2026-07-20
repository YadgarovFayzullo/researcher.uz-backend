from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from src.api.router import api_router
from src.core.config import settings
from src.infrastructure.persistence.db import DatabaseUnavailableError

app = FastAPI(title=settings.PROJECT_NAME)

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
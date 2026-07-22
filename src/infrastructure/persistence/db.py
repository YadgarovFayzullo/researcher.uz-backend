from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from src.core.config import settings

SQLALCHEMY_DATABASE_URL = settings.DATABASE_URL.replace(
    "postgresql+psycopg2://", "postgresql+asyncpg://"
)

engine = create_async_engine(
    SQLALCHEMY_DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
    connect_args={"ssl": False},
)

AsyncSessionLocal = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class DatabaseUnavailableError(Exception):
    """Raised when the application cannot establish a database connection."""

class Base(DeclarativeBase):
    pass


async def get_db():
    # Раньше здесь на КАЖДЫЙ запрос делался лишний SELECT 1 (+retry-цикл) как
    # проверка живости соединения — но это уже делает pool_pre_ping=True при
    # выдаче коннекта из пула. Не дублируем: минус один round-trip к БД на запрос.
    session = AsyncSessionLocal()
    try:
        yield session
    finally:
        await session.close()

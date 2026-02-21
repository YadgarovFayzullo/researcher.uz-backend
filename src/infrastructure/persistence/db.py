import asyncio
from sqlalchemy import text
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
    retries = 5
    session: AsyncSession | None = None

    for i in range(retries):
        session = AsyncSessionLocal()
        try:
            await session.execute(text("SELECT 1"))
            break
        except Exception:
            await session.rollback()
            await session.close()
            if i < retries - 1:
                print(f"[DB] Connection failed ({i + 1}/{retries}), retrying...")
                await asyncio.sleep(3)
                continue
            raise ConnectionError("Cannot connect to the database after multiple retries")

    if session is None:
        raise ConnectionError("Cannot initialize database session")

    try:
        yield session
    finally:
        await session.close()

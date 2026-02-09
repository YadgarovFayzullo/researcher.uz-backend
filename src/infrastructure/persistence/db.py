import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from src.core.config import settings

SQLALCHEMY_DATABASE_URL = settings.DATABASE_URL.replace(
    "postgresql+psycopg2://", "postgresql+asyncpg://"
)

engine = create_async_engine(
    SQLALCHEMY_DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"ssl": False},
)

AsyncSessionLocal = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    retries = 5
    for i in range(retries):
        try:
            async with AsyncSessionLocal() as session:
                yield session
            break
        except Exception as e:
            print(f"[DB] Connection failed ({i + 1}/{retries}), retrying...")
            await asyncio.sleep(3)
    else:
        raise ConnectionError("Cannot connect to the database after multiple retries")

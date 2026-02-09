import asyncio
import sys
from sqlalchemy.ext.asyncio import create_async_engine
from src.core.config import settings

# Force reload of settings to pick up new env vars if needed
from pydantic_settings import BaseSettings
class Settings(BaseSettings):
    PROJECT_NAME: str = "Scientific Backend"
    DATABASE_URL: str
    SECRET_KEY: str
    class Config:
        env_file = (".env", ".env.local")
        env_file_encoding = "utf-8"

print(f"Testing connection to: {Settings().DATABASE_URL}")

async def check_connection():
    url = Settings().DATABASE_URL.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            print("Successfully connected to the database!")
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)
    finally:
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(check_connection())

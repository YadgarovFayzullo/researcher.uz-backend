import asyncio
import traceback
from sqlalchemy import text
from src.infrastructure.persistence.db import engine, SQLALCHEMY_DATABASE_URL
from src.core.config import settings

async def test_connection():
    try:
        print(f"Settings DATABASE_URL: {settings.DATABASE_URL}")
        print(f"Final SQLAlchemy URL: {SQLALCHEMY_DATABASE_URL}")
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            print(f"Connection successful! Result: {result.scalar()}")
    except Exception:
        traceback.print_exc()
    finally:
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(test_connection())

import asyncio
from src.infrastructure.persistence.db import engine, Base
from src.infrastructure.persistence.models import Article, Journal

async def create_tables():
    print("Connecting to database to recreate tables...")
    async with engine.begin() as conn:
        # Drop all tables first to apply new schema changes
        await conn.run_sync(Base.metadata.drop_all)
        # Recreate tables with new columns (like slug)
        await conn.run_sync(Base.metadata.create_all)
    print("Tables recreated successfully with new columns!")

if __name__ == "__main__":
    asyncio.run(create_tables())

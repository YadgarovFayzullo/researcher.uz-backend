import asyncio
import asyncpg
import sys

# URL forced to 127.0.0.1 and no SSL
DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:5432/scientific_db"


async def test_connection():
    print(f"Connecting to {DATABASE_URL}...")
    try:
        conn = await asyncpg.connect(DATABASE_URL, ssl=False)
        version = await conn.fetchval("SELECT version()")
        print(f"Connected! Server version: {version}")
        await conn.close()
    except Exception as e:
        print(f"Failed: {e}")
        # Print detailed traceback
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(test_connection())

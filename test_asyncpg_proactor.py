import asyncio
import asyncpg
import sys

# Try both DBs to be sure
URLS = [
    "postgresql://postgres:postgres@localhost:5432/scientific_db",
    "postgresql://postgres:postgres@127.0.0.1:5432/scientific_db"
]

async def test_single(url):
    print(f"Testing {url}...")
    try:
        conn = await asyncpg.connect(url, ssl=False)
        version = await conn.fetchval("SELECT version()")
        print(f"SUCCESS! Version: {version}")
        await conn.close()
        return True
    except Exception as e:
        print(f"Request failed: {e}")
        return False

async def main():
    print(f"Python {sys.version}")
    # Default loop on Windows 3.13 is ProactorEventLoop
    loop = asyncio.get_running_loop()
    print(f"Event Loop: {type(loop).__name__}")
    
    for url in URLS:
        if await test_single(url):
            break

if __name__ == "__main__":
    # Do NOT set policy, let it use default (Proactor on Windows)
    asyncio.run(main())

import asyncio
from sqlalchemy import text
from src.infrastructure.persistence.db import engine
from src.infrastructure.persistence.models import Journal, Issue


async def seed_data():
    print("Seeding initial data...")
    async with engine.begin() as conn:
        # 1. Создаем тестовый журнал
        journal = Journal(
            name="Scientific Journal of AI",
            slug="scientific-journal-ai",
            description="Testing journal",
        )

        # 2. Создаем выпуск для этого журнала
        # Так как мы в engine.begin(), мы можем использовать сессию или просто выполнить SQL
        # Но проще через сессию. Давайте сделаем это через сессию

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import sessionmaker

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        # Проверяем, есть ли уже данные
        from sqlalchemy import select

        res = await session.execute(select(Journal).limit(1))
        if res.scalar():
            print("Data already exists. Skipping...")
            return

        test_journal = Journal(
            name="Scientific Journal of AI",
            slug="scientific-journal-ai",
            description="Testing journal",
            issn="1234-5678",
        )
        session.add(test_journal)
        await session.flush()  # Получаем ID журнала

        test_issue = Issue(journal_id=test_journal.id, year=2024, volume="9", issue="3")
        session.add(test_issue)
        await session.commit()
        print(
            f"Success! Created Journal ID: {test_journal.id}, Issue ID: {test_issue.id}"
        )


if __name__ == "__main__":
    asyncio.run(seed_data())

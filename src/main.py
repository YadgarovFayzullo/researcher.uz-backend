from fastapi import FastAPI
from src.api.router import api_router
from src.core.config import settings

app = FastAPI(title=settings.PROJECT_NAME)

app.include_router(api_router)

@app.get("/")
async def root():
    return {"message": "Welcome to Scientific Backend API"}
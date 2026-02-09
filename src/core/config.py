from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "Scientific Backend"
    DATABASE_URL: str
    SECRET_KEY: str

    class Config:
        env_file = (".env", ".env.local")
        env_file_encoding = "utf-8"


settings = Settings()

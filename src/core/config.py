from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "Scientific Backend"
    DATABASE_URL: str
    SECRET_KEY: str  # используется для подписи JWT

    # --- JWT / сессии ---
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60  # 1 час
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # --- Cookie ---
    # httpOnly-cookie, которые читает Next.js middleware (Фаза 8).
    COOKIE_SECURE: bool = False          # True в проде (https)
    COOKIE_SAMESITE: str = "lax"
    COOKIE_DOMAIN: str | None = None     # напр. ".researcher.uz" в проде
    ACCESS_COOKIE_NAME: str = "access_token"
    REFRESH_COOKIE_NAME: str = "refresh_token"

    # Куда редиректить фронт после OAuth-callback.
    FRONTEND_URL: str = "http://localhost:3000"

    # Origin'ы, которым разрешён CORS с credentials. Wildcard "*" здесь
    # невозможен: браузер не отправляет cookie на ответ с Allow-Origin: *,
    # а вся сессия у нас именно в httpOnly-cookie. Список через запятую.
    CORS_ORIGINS: str = "http://localhost:3000"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    # --- Google OAuth (основной вход, 80/86 юзеров) ---
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    GOOGLE_REDIRECT_URI: str | None = None  # напр. http://localhost:8000/auth/google/callback

    # --- ORCID OAuth (привязка/импорт работ, не основной вход) ---
    ORCID_CLIENT_ID: str | None = None
    ORCID_CLIENT_SECRET: str | None = None
    ORCID_REDIRECT_URI: str | None = None
    ORCID_ENV: str = "production"  # 'sandbox' | 'production'

    # OpenAlex (цитирования) — ключ не нужен, mailto = polite pool
    OPENALEX_MAILTO: str = "info@researcher.uz"

    # --- Cloudflare R2 (Storage, Фаза 6) — S3-совместимо ---
    R2_ACCOUNT_ID: str | None = None
    R2_ACCESS_KEY_ID: str | None = None
    R2_SECRET_ACCESS_KEY: str | None = None
    R2_BUCKET: str | None = None
    # Публичный базовый URL раздачи (r2.dev или кастомный домен), напр.
    # https://files.researcher.uz — без завершающего слэша.
    R2_PUBLIC_BASE_URL: str | None = None

    @property
    def R2_ENDPOINT(self) -> str | None:
        if not self.R2_ACCOUNT_ID:
            return None
        return f"https://{self.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

    class Config:
        env_file = (".env", ".env.local")
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()

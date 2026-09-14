import json

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
    # 3001 — потому что Next сам переезжает на него, когда 3000 занят другим
    # проектом; без этого браузер режет запросы как cross-origin.
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3001"

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

    # --- Cloudflare Turnstile (антибот) ---
    # Пока секрет пуст, проверка выключена целиком (см. src/core/turnstile.py).
    TURNSTILE_SECRET_KEY: str | None = None
    # Cookie-«пропуск человека»: сколько живёт вердикт капчи, чтобы поиск не
    # дёргал виджет на каждый запрос.
    HUMAN_COOKIE_NAME: str = "human_pass"
    HUMAN_PASS_EXPIRE_HOURS: int = 12

    # --- Статистика: чьи взаимодействия не считаем (см. src/core/bots.py) ---
    # Сети ферм headless-браузеров, подставляющих UA обычного браузера. Через
    # запятую; менять можно переменной окружения, без выкатки кода.
    # 43.172.0.0/15 — Tencent Cloud Singapore, август 2026.
    STATS_BLOCKED_NETWORKS: str = "43.172.0.0/15"

    # --- Защита раздачи PDF от массовой выкачки (см. src/core/pdf_guard.py) ---
    # Сколько РАЗНЫХ файлов один IP может забрать за окно. Считаем файлы, а не
    # запросы: просмотрщик тянет один PDF десятком Range-запросов.
    PDF_RATE_LIMIT: int = 30
    PDF_RATE_WINDOW_SECONDS: int = 3600
    # На сколько закрывается доступ после превышения.
    PDF_BAN_SECONDS: int = 3600
    # Где лежит счётчик выдач. Файл общий для воркеров uvicorn — в памяти
    # процесса он дал бы лимит, умноженный на число воркеров.
    PDF_GUARD_DB: str = "/tmp/pdf_guard.sqlite3"
    # Общий секрет с прокси фронта (`/pdf/<slug>.pdf` на Vercel ходит сюда сам,
    # и без него мы видели бы IP Vercel, а не читателя). Пусто — доверяем
    # только адресу сокета.
    PDF_PROXY_SECRET: str | None = None

    # --- OAI-PMH (выдача метаданных партнёрам: EBSCO, BASE, DOAJ) ---
    # Эндпоинт /oai закрыт ключом из таблицы oai_clients — публичного доступа
    # нет вообще, поэтому «выключателя» здесь нет: нет выданных ключей = никто
    # ничего не заберёт.
    OAI_REPOSITORY_NAME: str = "researcher.uz"
    OAI_ADMIN_EMAIL: str = "info@researcher.uz"
    # Часть идентификатора oai:<namespace>:article/<id>. Менять нельзя после
    # первой выдачи наружу: харвестер узнаёт записи именно по этой строке.
    OAI_NAMESPACE: str = "researcher.uz"
    # baseURL, который мы объявляем в ответах. Пусто — берём из самого запроса.
    OAI_BASE_URL: str | None = None
    # Размер страницы выдачи. Заодно ограничивает темп выкачки: 100 записей за
    # запрос при лимите OAI_RATE_LIMIT.
    OAI_PAGE_SIZE: int = 100
    # Сколько живёт resumptionToken (сек). Дольше суток харвестеру не нужно, а
    # протухший токен заставит его начать заново — это штатное поведение.
    OAI_TOKEN_TTL_SECONDS: int = 86400
    # Лимит запросов к /oai с одного IP (формат slowapi).
    OAI_RATE_LIMIT: str = "60/minute"

    # --- Crossref: регистрация DOI (crossref-integration.md) ---
    # Логин/пароль МЕМБЕРА Crossref (не префикс — префикс без учётки бесполезен).
    # Пусто = депозит выключен целиком: домен собирает XML и показывает превью,
    # но отправить его некуда, и ручки отвечают 503.
    CROSSREF_LOGIN_ID: str | None = None
    CROSSREF_LOGIN_PASSWORD: str | None = None
    # Учётки отдельных издателей. Префикс принадлежит МЕМБЕРУ: под чужой
    # префикс нашим логином не задепонируешь, Crossref отвергнет батч. Поэтому
    # журнал со своим членством (`journals.metadata.crossref_account = "<key>"`)
    # ходит под своей учёткой. JSON: {"<key>": {"login": "...", "password": "..."}}.
    # Хранится в env, а не в БД: journals.metadata читается публично.
    CROSSREF_ACCOUNTS: str | None = None
    # 'test' — песочница test.crossref.org, DOI НЕ регистрируются по-настоящему;
    # 'production' — боевой doi.crossref.org. Депозит необратим: обновить DOI
    # можно, отозвать — нет, поэтому по умолчанию песочница.
    CROSSREF_ENV: str = "test"
    # Кто депонирует (уходит в <head>): организация и адрес, куда Crossref шлёт
    # отчёты о батчах. registrant — правообладатель метаданных, обычно издатель.
    CROSSREF_DEPOSITOR_NAME: str = "researcher.uz"
    CROSSREF_DEPOSITOR_EMAIL: str = "info@researcher.uz"
    CROSSREF_REGISTRANT: str = "researcher.uz"
    # Префикс по умолчанию (10.XXXXX). Обычно у каждого журнала свой — он лежит
    # в journals.metadata.doi_prefix; сюда попадает только платформенный.
    CROSSREF_DEFAULT_PREFIX: str | None = None
    # Суффикс DOI. Детерминирован от id статьи: slug меняется, id — нет, а DOI
    # не переприсваивают. Подстановки: {article_id}, {issue_id}, {year}.
    CROSSREF_DOI_SUFFIX_TEMPLATE: str = "ruz.{article_id}"

    @property
    def CROSSREF_BASE_URL(self) -> str:
        # Песочница живёт на отдельном хосте с тем же API и отдельной учёткой.
        if self.CROSSREF_ENV == "production":
            return "https://doi.crossref.org"
        return "https://test.crossref.org"

    @property
    def CROSSREF_ENABLED(self) -> bool:
        return bool(
            (self.CROSSREF_LOGIN_ID and self.CROSSREF_LOGIN_PASSWORD)
            or self.crossref_accounts
        )

    @property
    def crossref_accounts(self) -> dict[str, dict]:
        """Учётки издателей из CROSSREF_ACCOUNTS. Кривой JSON = пусто, а не
        падение импорта: без него остальной бэкенд обязан работать."""
        if not self.CROSSREF_ACCOUNTS:
            return {}
        try:
            parsed = json.loads(self.CROSSREF_ACCOUNTS)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

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

    # --- Автопроверка выпуска перед публикацией (issue-review.md) ---
    # Главный выключатель. False = поведение как до фичи: статья из формы
    # публикуется сразу, планировщик не стартует, ничего не проверяется.
    ISSUE_REVIEW_ENABLED: bool = True
    # Какие слои проверки работают:
    #   rules — только алгоритмические (src/domain/issue_checks.py): пересечение
    #           страниц, дубли DOI/заголовков/файлов, формат метаданных, грубая
    #           сверка с текстом PDF. Денег не стоят, работают без ключа.
    #   llm   — только сверка метаданных с PDF моделью.
    #   both  — оба (по умолчанию). Без ANTHROPIC_API_KEY автоматически
    #           вырождается в rules.
    ISSUE_REVIEW_MODE: str = "both"

    @property
    def review_uses_rules(self) -> bool:
        return self.ISSUE_REVIEW_ENABLED and self.ISSUE_REVIEW_MODE in ("rules", "both")

    @property
    def review_uses_llm(self) -> bool:
        # Ключ любого из провайдеров включает слой; какого именно — решает
        # src/infrastructure/external/llm.py.
        return (
            self.ISSUE_REVIEW_ENABLED
            and self.ISSUE_REVIEW_MODE in ("llm", "both")
            and bool(
                self.GEMINI_API_KEY
                or self.OPENROUTER_API_KEY
                or self.ANTHROPIC_API_KEY
            )
        )

    @property
    def REVIEW_ACTIVE(self) -> bool:
        """Есть ли хоть один рабочий слой — от этого зависит, придерживать ли
        статью черновиком и запускать ли планировщик."""
        return self.review_uses_rules or self.review_uses_llm

    # Чья модель отвечает: gemini | openrouter | anthropic. Пусто —
    # определяется по тому, чей ключ задан (см. llm.provider()).
    LLM_PROVIDER: str | None = None

    # Google AI Studio: бесплатный тариф с суточными лимитами, ключ на
    # aistudio.google.com. Для сверки метаданных с PDF этого хватает.
    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # OpenRouter: витрина чужих моделей, среди них бесплатные (суффикс
    # `:free`). Модель задаётся явно — список бесплатных меняется, и жёстко
    # вшитое имя однажды перестанет существовать.
    OPENROUTER_API_KEY: str | None = None
    OPENROUTER_MODEL: str | None = None

    ANTHROPIC_API_KEY: str | None = None
    # Модель Claude, если выбран этот провайдер. Дефолт — Opus 5; на большом
    # потоке имеет смысл переключить на claude-sonnet-5, не трогая код.
    AI_REVIEW_MODEL: str = "claude-opus-5"
    # Сколько ждать после ПОСЛЕДНЕЙ залитой в выпуск статьи, прежде чем
    # проверять. Редактор заливает номер порциями по 10-20 статей; проверять
    # после каждой значит проверять недособранный выпуск.
    AI_REVIEW_DELAY_MINUTES: int = 60
    # Как часто планировщик смотрит, не дозрел ли выпуск.
    AI_REVIEW_POLL_SECONDS: int = 300
    # Сколько страниц PDF отдаём модели. Метаданные (заголовок, авторы,
    # аннотация, колонтитул с журналом и страницами) живут на первых двух;
    # дальше идёт тело статьи, за которое незачем платить.
    AI_REVIEW_PDF_PAGES: int = 3
    # Сколько статей проверяем параллельно.
    AI_REVIEW_CONCURRENCY: int = 4
    # Сколько раз пробуем статью, если API недоступен. Исчерпав попытки, не
    # публикуем и не блокируем — пишем владельцу, что проверить не смогли.
    AI_REVIEW_MAX_ATTEMPTS: int = 3
    # Обязателен ли ответ модели для автопубликации. False (по умолчанию):
    # если модель недоступна — кончились кредиты, лежит API, — но слой правил
    # отработал и молчит, статья публикуется. Иначе каждый сбой биллинга
    # превращался бы в поток «не проверено» и ручную работу владельцу, хотя
    # проверка де-факто прошла. True — строгий режим: без вердикта модели
    # ничего не публикуется.
    AI_REVIEW_REQUIRED: bool = False

    # --- Загрузка папки PDF редактором (import-integration.md, источник C) ---
    # Метаданные читаются из самих файлов тем же провайдером LLM_PROVIDER.
    # Модель Claude для этого — отдельная от AI_REVIEW_MODEL: разбор шапки —
    # перенос текста в поля, Haiku с ним справляется втрое-впятеро дешевле.
    IMPORT_EXTRACT_MODEL: str = "claude-haiku-4-5"
    # Сколько первых страниц читаем: многоязычные журналы печатают по странице
    # шапки на язык, дальше идёт тело статьи.
    IMPORT_EXTRACT_PAGES: int = 3
    # Сколько файлов распознаём параллельно. Бесплатный тариф Gemini режет
    # частоту запросов, поэтому немного.
    IMPORT_EXTRACT_CONCURRENCY: int = 2
    # Попыток на файл, если провайдер не ответил (429, 5xx, таймаут).
    IMPORT_EXTRACT_ATTEMPTS: int = 3

    # --- Telegram: уведомления владельцу о результатах проверки ---
    # Бот шлёт разбор проблемного выпуска с кнопками «опубликовать» /
    # «оставить закрытым». Пусто = уведомления выключены (выпуск всё равно
    # будет погашен, просто молча — увидите в админке).
    TELEGRAM_BOT_TOKEN: str | None = None
    # Чей чат считаем чатом владельца. Кнопки принимаются только от него.
    TELEGRAM_OWNER_CHAT_ID: str | None = None
    # Общий секрет вебхука: Telegram шлёт его в X-Telegram-Bot-Api-Secret-Token.
    # Без него адрес вебхука — единственная защита ручки, а он утекает в логи
    # прокси. Ставится тем же скриптом, что и сам вебхук.
    TELEGRAM_WEBHOOK_SECRET: str | None = None
    # Базовый адрес фронта для ссылок «открыть в админке» в сообщении бота.
    ADMIN_BASE_URL: str = "https://researcher.uz"

    # --- Рассылка авторам статей (src/domain/outreach.py) ---
    # Отправляет только scripts/outreach_send.py и только с --send. Без ключа и
    # отправителя скрипт работает в режиме просмотра.
    RESEND_API_KEY: str | None = None
    # «Имя Фамилия, researcher.uz <name@mail.researcher.uz>» — отдельный
    # поддомен, чтобы жалобы на рассылку не били по транзакционной почте.
    OUTREACH_FROM: str | None = None
    # Живой ящик: ответы «это не мои работы» разбираются руками.
    OUTREACH_REPLY_TO: str | None = None
    # Кто подписывает письмо. Письмо от человека, свёрстанное простыми
    # абзацами, Gmail заметно чаще кладёт в «Несортированные», а не в
    # «Оповещения», куда уходит всё, что похоже на рассылку сервиса.
    OUTREACH_SIGNER: str = "researcher.uz jamoasi"
    # Отправитель писем в ответ на действие человека (одобрение заявки и т.п.).
    # Пусто — берётся OUTREACH_FROM.
    NOTIFY_FROM: str | None = None
    # Секрет подписи вебхука Resend (whsec_…), выдаётся при создании вебхука в
    # панели Resend. Пусто — ручка /outreach/webhooks/resend отвечает 503.
    RESEND_WEBHOOK_SECRET: str | None = None
    # Куда ведут ссылки письма (страница автора) и где живёт ручка отписки.
    OUTREACH_SITE_URL: str = "https://researcher.uz"
    OUTREACH_API_URL: str = "https://api.researcher.uz"

    @property
    def TELEGRAM_ENABLED(self) -> bool:
        return bool(self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_OWNER_CHAT_ID)

    class Config:
        env_file = (".env", ".env.local")
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()

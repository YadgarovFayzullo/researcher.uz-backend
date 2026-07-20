# План миграции researcher.uz: Supabase → Python (FastAPI + свой Postgres)

Статус документа: черновик v1 · Цель: полный уход от Supabase (БД, Auth, Storage, RLS, RPC) на самостоятельный стек `FastAPI + SQLAlchemy(async) + свой PostgreSQL`.

Решения владельца (зафиксированы):
- **База:** свой Postgres + миграция данных из Supabase (полная независимость).
- **Первый артефакт:** этот план.

Фронтенд (Next.js на `researcher-uz`) сохраняется и переключается с прямых вызовов Supabase на новый REST API.

---

## 0. Текущее состояние (факт)

**Backend `researcher.uz-backend`** — ранний каркас, закрывает ~10%:
- FastAPI + async SQLAlchemy + asyncpg + Alembic, подключение к `localhost:5432`, retry + 503.
- Модели: `journals`, `issues`, `articles`, `article_interactions` — **и те отстают** от `schema.sql`.
- Эндпоинты: CRUD журналов/статей по slug, статистика (view/like).
- `Storage.save_pdf` — заглушка (`pass`). Auth/authz/RPC/search/embeddings/payments — нет.

**Frontend `researcher-uz`** — Next.js 15, **75 файлов** импортируют Supabase-клиент; 4-уровневая клиентская модель; 10 API-routes; ~24 RPC-вызова; Storage-бакет `pdfs`; middleware с auth-гейтингом.

### 0.1 Инвентарь таблиц (schema.sql = источник истины)

| Таблица | Модель в backend | Действие |
|---|---|---|
| `journals` | ✅ (устарела) | обновить: `type`, `metadata`, `subject_codes`, `printed_issn` |
| `issues` | ✅ (устарела) | обновить: `title`, `date_start/end`, `location`, `isbn`, `cover_image`, `metadata` |
| `articles` | ✅ (устарела) | обновить: `publication_type`, `isbn`, `publisher`, `publisher_id`, `section_id`, `admin_id`, `cover_image`, `publication_year`, `metadata`, `embedding` |
| `article_interactions` | ✅ | ок |
| `profiles` | ❌ | добавить (ключевая — роли, orcid) |
| `journal_admins` | ❌ | добавить (authz) |
| `publishers` | ❌ | добавить |
| `conference_sections` | ❌ | добавить |
| `article_authors` | ❌ | добавить |
| `article_references` | ❌ | добавить |
| `external_citations` | ❌ | добавить |
| `researcher_works` | ❌ | добавить |
| `saved_articles` | ❌ | добавить |
| `affiliations` | ❌ | добавить |
| `research_interests` | ❌ | добавить |
| `social_links` | ❌ | добавить |
| `achievements` | ❌ | добавить |
| `profile_stats` | ❌ | добавить |
| `auth.users` (Supabase) | ❌ | заменить своей `users` (см. Фаза 3) |

Итого: **4 из 19** есть, все 4 требуют правок.

### 0.2 Инвентарь RPC (24 функции → сервисы Python)

| RPC | Назначение | Куда переносим |
|---|---|---|
| `search_articles` | полнотекст-поиск | `services/search.py` (Фаза 7) |
| `add_interaction` / `increment_article_views` | счётчики | `services/stats.py` (частично есть) |
| `get_article_stats` / `get_journal_stats` / `get_platform_stats` / `get_daily_stats` / `get_journal_analytics` / `get_top_articles` | агрегаты | `services/stats.py` (Фаза 5) |
| `get_all_profiles` / `update_my_profile` | профили | `services/profiles.py` |
| `set_user_role` / `set_journal_admin` / `get_all_journal_admins` | роли/привязки (owner-only) | `services/admin.py` + authz-гард (Фаза 4) |
| `get_editor_options` | справочники | `services/admin.py` |
| `claim_article` / `unclaim_article` | привязка автора | `services/articles.py` |
| `link_my_orcid` / `import_orcid_works` / `get_researcher_profile` | ORCID | `services/orcid.py` (Фаза 5) |
| `get_article_citations` / `get_citing_articles` / `match_articles_by_doi` / `upsert_external_citations` | цитирования | `services/citations.py` (Фаза 5) |

> При переносе воспроизвести логику `SECURITY DEFINER` через явные проверки прав в сервисе (не полагаться на RLS).

### 0.3 Что в Supabase «бесплатно», а здесь пишем руками
Auth (сессии/OAuth), RLS (авторизация в БД), Storage (`pdfs`), полнотекст на 5 языков (`document_ru/en/uz`, `search_vector`), `pgvector` embeddings. _(Платежи Multicard в миграцию не входят — см. Фаза 8.)_

---

## Принципы

1. **Strangler-fig, а не big-bang.** Backend поднимается параллельно, фронт переключается по доменам (сначала read-only публичные страницы, в конце — auth и платежи). До финального cutover обе системы работают.
2. **Схема — из `schema.sql`.** Модели SQLAlchemy = точная копия существующей схемы, чтобы `pg_dump` данных лёг без конвертаций.
3. **Каждая фаза с критерием приёмки** (эндпоинт отвечает / данные совпадают / тест зелёный) — иначе не переходим дальше.
4. **Auth — самый рисковый кусок**, проектируется рано (Фаза 3), но переключается на нём фронт в предпоследнюю очередь.

---

## Фаза 1 — Модель данных (фундамент) ✅ ГОТОВО
**Цель:** все 19 таблиц как SQLAlchemy-модели, 1:1 со `schema.sql`.

- [x] Обновить `Journal`, `Issue`, `Article` до актуальных колонок (см. 0.1). _(колонка `metadata` → атрибут `meta` — `metadata` зарезервировано в Declarative.)_
- [x] Добавить недостающие модели: `profiles`, `publishers`, `journal_admins`, `conference_sections`, `article_authors`, `article_references`, `external_citations`, `researcher_works`, `saved_articles`, `affiliations`, `research_interests`, `social_links`, `achievements`, `profile_stats` **+ `users` и `identities`** (замена `auth.users`/`auth.identities`, Фаза 3). FK, ранее указывавшие на `auth.users`, → `users.id`.
- [x] Спец-типы: `ARRAY(Text)` (`subject_codes`), `JSONB` (`metadata`), `TSVECTOR`, `Vector()` из `pgvector` (`articles.embedding`, без фикс. размерности — в проде не задана), `INET`.
- [x] `CHECK`-констрейнты (orcid-regex ×2, `publication_type` enum, `journals.type`, publishers slug-regex, `data <= CURRENT_DATE`) — все 6 в БД.
- [x] Расширения БД: `CREATE EXTENSION vector; pg_trgm; unaccent` в начале Alembic-`upgrade()`. **docker-compose db → образ `pgvector/pgvector:pg17`** (стоковый postgres не содержит `vector`).
- [x] Пересобрана Alembic-миграция (`2fd04aeedf1b_full_schema_from_schema_sql.py`) — 20 таблиц; `env.py`/`create_tables.py` импортируют весь модуль моделей; `pgvector` в requirements.

**Приёмка:** ✅ `alembic upgrade head` на чистом Postgres создаёт 20 таблиц + 3 расширения; `alembic check` → «No new upgrade operations detected» (модели == БД, дрейфа нет).
**Оценка:** 2–3 дня. **Риск:** низкий. **Факт:** сделано.

> **Осталось в рамках паритета со `schema.sql` (перенести на Фазу 2/7):** индексы (GIN на tsvector, unique на slug'ах), триггеры пересборки `document_*`/`search_vector`, sequence-restart для IDENTITY после COPY. Схема таблиц/типов/констрейнтов — готова.

---

## Фаза 2 — Миграция данных из Supabase ✅ ГОТОВО
**Цель:** перенести все данные (кроме `auth.*`) в свой Postgres.

**Способ:** не `pg_dump` (нет пароля к Postgres Supabase), а ETL по API с имеющимся
service_role-ключом — скрипт [`scripts/migrate_from_supabase.py`](scripts/migrate_from_supabase.py):
- `auth.users` → своя таблица `users` (id, email, email_confirmed_at, created_at) через **Admin Auth API** (`/auth/v1/admin/users`); `password_hash` пока NULL — 9 bcrypt-хэшей добираются в Фазе 3.
- identities → таблица `identities` через per-user `/auth/v1/admin/users/{id}` (list-эндпоинт их НЕ отдаёт). Google `sub` лежит в поле `id` identity (не в `provider_id`).
- `public.*` → через **PostgREST** (`/rest/v1/<table>`, пагинация Range по 1000).
- На время загрузки `session_replication_role='replica'` (postgres = superuser) — FK/триггеры off, порядок вставки не критичен; identity-колонки через `OVERRIDING SYSTEM VALUE`.
- tsvector-колонки (`document_*`, `search_vector`) НЕ переносятся — пересборка в Фазе 7.

- [x] Целевая БД унифицирована: **`scientific_db`** (совпадает с `.env` бэкенда); схема Фазы 1 накачена туда, дубликат в БД `postgres` снесён.
- [x] Загружены `users`=86, `identities`=89 (**80 google + 9 email** — совпадает с ожиданием).
- [x] Загружены все 18 `public`-таблиц.
- [x] **FK-целостность: 0 сирот** по всем 15 связям (`profiles.id→users`, `articles.*→profiles/issues/publishers/sections`, `article_authors/interactions/references/external_citations→articles`, `journal_admins.user_id→users` и т.д.).
- [x] Сверка счётчиков (источник = цель): journals 19, issues 48, articles 1937, article_authors 2193, external_citations 1000, profile_stats 72, journal_admins 18, researcher_works 15, publishers 1, conference_sections 5, saved_articles 2, affiliations 1, research_interests 1, achievements/social_links 0. `article_interactions` 72925 vs источник 72926 — **живой append-only лог просмотров растёт**, цель — строгое подмножество (финальная дельта — при переключении, Фаза 9).
- [x] IDENTITY-sequences выставлены на `max(id)` (articles→2182, issues→81, journals→30, publishers→1, conference_sections→5) — новые вставки не словят конфликт PK.

**Приёмка:** ✅ `count(*)` совпадает (кроме +1 живого interaction); спот-проверка 5 журналов + 10 статей — поля идентичны (id/slug/name|title/type/publication_type/issue_id).
**Оценка:** 1–2 дня. **Риск:** средний.

**Особенности/долги на потом:**
- Пароли (9 bcrypt) требуют доступа к `auth.users.encrypted_password` — прямой коннект к Postgres Supabase (Dashboard → Settings → Database) или SQL-выгрузка через админку → Фаза 3.
- Повторный запуск ETL идемпотентен (`ON CONFLICT DO NOTHING`), но перед боевым переключением нужен финальный дельта-прогон (особенно `article_interactions`).

---

## Фаза 3 — Auth (самый рисковый блок) 🟡 КОД ГОТОВ (осталось: секреты OAuth + добор 9 хэшей)
**Цель:** своя аутентификация вместо Supabase Auth.

**Факт по проду (86 юзеров): 80 через Google OAuth, 9 через email+пароль (у 3 оба).** → **Google OAuth — основной способ входа (93%)**, приоритет №1; email/password — второстепенный, но хеши переносим.

**Стратегия (решение владельца):** строим всё, что без внешних блокеров, сейчас; единственная внешняя зависимость — 9 bcrypt-хэшей (нужен прямой коннект к Postgres Supabase) — уезжает в самый конец, к финальному переключению.

- [x] Таблица `users` (замена `auth.users`) и `identities` — из Фазы 1; данные (86/89) залиты в Фазе 2. `id` сохранены из `auth.users`.
- [x] **Крипто-ядро** [`src/core/security.py`](src/core/security.py): bcrypt (`bcrypt` lib, формат `$2a$/$2b$/$2y$` — совместим с Supabase) + JWT HS256 (access `create_access_token` / refresh `create_refresh_token` / `decode_token` с проверкой типа). Cookie-хелперы [`src/core/cookies.py`](src/core/cookies.py) (httpOnly, для middleware Next.js).
- [x] **Домен** [`src/domain/auth.py`](src/domain/auth.py): `authenticate`, `register` (создаёт users+profiles+identities), `get_or_create_oauth_user` (ищет по identity → по email → создаёт; линкует несколько провайдеров на одного).
- [x] **Зависимости** [`src/api/deps.py`](src/api/deps.py): `get_current_user` (401) / `get_optional_user`; токен из `Authorization: Bearer` **или** httpOnly-cookie.
- [x] **Эндпоинты** [`src/api/v1/auth.py`](src/api/v1/auth.py): `POST /auth/register|login|refresh|logout`, `GET /auth/me`. Токены и в теле, и в cookie.
- [x] **🔴 Google OAuth** [`src/api/v1/google_auth.py`](src/api/v1/google_auth.py): `GET /auth/google/login` (state-cookie CSRF) → `GET /auth/google/callback` (code→token→userinfo→get_or_create по `sub`→JWT-cookie→redirect на фронт). Env: `GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI`.
- [x] **ORCID OAuth** [`src/api/v1/orcid_auth.py`](src/api/v1/orcid_auth.py) + [`src/domain/orcid.py`](src/domain/orcid.py): порт `api/orcid/callback` — обмен→профиль pub.orcid.org→`link_orcid` (пишет orcid+поля в profile и claim'ит `article_authors` по ORCID, аналог RPC `link_my_orcid`, вкл. проверку «orcid_taken»). Env: `ORCID_CLIENT_ID/SECRET/REDIRECT_URI`, `ORCID_ENV`.
- [ ] **⚠️ Внешний блокер → Фаза 9:** добор 9 bcrypt-хэшей из `auth.users.encrypted_password` в `users.password_hash` (прямой коннект к Postgres Supabase / SQL-выгрузка). До этого 9 email-юзеров входят через Google (если есть) или ждут ресета; парольный путь кода уже рабочий.
- [ ] **Настройка секретов (владелец):** завести Google OAuth-app в Google Cloud Console (раньше это делал Supabase) + ORCID app; вписать client_id/secret/redirect в `.env.local`. Без них `/auth/google/*` и `/auth/orcid/*` отдают 503 (остальной auth работает).
- [ ] Email (подтверждение/сброс) через Resend — перенести при необходимости (Фаза 5).

**Приёмка (факт на этой итерации):**
- (1) ✅ существующий Google-юзер → **тот же `user_id`**, дубликаты не создаются (проверено `get_or_create_oauth_user` на реальной перенесённой google-identity: `19c1b9c4…`, users/identities без прироста).
- (2) ⏸ 9 email-паролей — отложено (нужны хэши); парольный путь проверен на свежесозданном юзере (register→login→неверный пароль=401).
- (3) ✅ `GET /auth/me` возвращает профиль (по Bearer и по cookie); аноним → 401.
- (4) ✅ ORCID/Google login без секретов → 503 (guard), код callback'ов готов; реальный OAuth-обмен проверяется после ввода секретов.
- Прочее: `register`/`login`/`refresh`(cookie)/`logout` — все 200; тестовый юзер удалён, счётчики возвращены к 86/89.

**Оценка:** 1.5–2 недели. **Риск:** высокий (пароли, паритет cookie-сессии с middleware, сохранность `user_id`). **Факт:** код готов и протестирован; ждёт секретов OAuth + добора хэшей.

---

## Фаза 4 — Авторизация (RLS → код) ✅ ГОТОВО
**Цель:** перенести политики `rls_roles.sql`/`rls_content.sql` в FastAPI-dependencies.

- [x] Зависимости (`src/api/deps.py`): `get_current_user` → `get_current_profile` (гард заблокированного аккаунта) → `require_owner`. Row-level случаи (issue/article зависят от полей тела) — предикаты `src/domain/authz.py` (`can_write_journal/issue/article`, `is_journal_admin`, `is_publisher_admin`), вызываются в эндпоинтах явно.
- [x] Правила записи применены к живым write-эндпоинтам, которые раньше были БЕЗ auth: `journal.py` (create/update/delete → `require_owner`), `article.py` (create/update/delete → `can_write_article` по полям строки, update/delete грузят существующую строку = RLS `using`).
- [x] Owner-only мутации ролей — сервис `src/domain/admin.py` (порт `owner_console.sql`: `set_user_role`, `set_journal_admin`, `get_all_profiles`, `get_all_journal_admins`) + эндпоинты `src/api/v1/admin.py` (`/admin/*`, все под `require_owner`). Инварианты: роль только `admin/authenticated`, owner-строку менять нельзя, attach промоутит до `admin`. Двойной гард (`require_owner` + `caller_is_owner`).
- [x] Роль `authenticated`; любое иное значение (истор. `user`) = блок (403 в `get_current_profile`).

**Приёмка:** ✅ `tests/verify_authz.py` — матрица прав (owner/journal-admin/publisher-admin/plain × journals/issues/articles) + инварианты AdminDomain: **29/29 checks passed** против реальной БД, фикстуры удаляются за собой (проверено 0 остатков).
**Оценка:** 4–6 дней. **Риск:** высокий (легко открыть дыру, которую раньше закрывал RLS). — сделано, дыра во write-эндпоинтах закрыта.

**Осталось (при доводке эндпоинтов Фазы 5/8):** issue-эндпоинтов пока нет в API — гард `can_write_issue` готов, подключить при их появлении; `ArticleCreate` пока не принимает `admin_id/publisher_id` (гард уже читает их через getattr — заработает, когда схема начнёт их нести).

---

## Фаза 5 — RPC → сервисы ✅ ГОТОВО
**Цель:** воспроизвести 24 RPC (см. 0.2) как Python-сервисы + эндпоинты.

- [x] Статистика (`src/domain/stats.py`, `/stats/*`): `get_article_stats` (батч `POST /stats/articles`), `get_journal_stats` (`GET /stats/journals`), `get_platform_stats` (`GET /stats/platform`), `get_journal_analytics`/`get_top_articles`/`get_daily_stats` (`POST /stats/journal-analytics|top-articles|daily-stats`), `increment_article_views` (`POST /stats/increment-views/{id}`), `add_interaction` (`POST /stats/interaction`). Соглашение как в SQL: строка = один признак `=1`, «views» = `count filter (view=1)`.
- [x] Профили/роли: `get_all_profiles`/`set_user_role`/`set_journal_admin`/`get_all_journal_admins` — уже в Фазе 4 (`src/domain/admin.py`); добавлен `get_editor_options` (`GET /admin/editor-options`, owner-only). `update_my_profile` — в кабинете (см. ниже).
- [x] Кабинет исследователя (`src/domain/researcher.py`, `/researcher/*`): `update_my_profile` (`PATCH /researcher/me/profile`, NULL=не трогать/''=очистить), `claim_article`/`unclaim_article` (`POST|DELETE /researcher/me/claim/{id}`), `claim_articles_by_dois` (`POST /researcher/me/claim-by-dois`), `import_orcid_works` (`POST /researcher/me/import-works`, full-replace), `get_researcher_profile` + внешние работы (`GET /researcher/{orcid}`, публичное, без id/role/email). `match_articles_by_doi` — в цитированиях.
- [x] ORCID: `link_my_orcid` — Фаза 3 (`src/domain/orcid.py`); `import_orcid_works`/`get_researcher_profile` — в кабинете.
- [x] Цитирования (`src/domain/citations.py`, `/citations/*`): `get_article_citations` (`POST /citations/counts`, гибрид `GREATEST(internal,external)`), `get_citing_articles` (`GET /citations/citing/{id}`), `match_articles_by_doi` (`POST /citations/match-doi`), `upsert_external_citations` (`POST /citations/external`, owner-only, двойной гард). Интеграция OpenAlex портирована из `/api/citations/refresh` + `src/lib/openalex.ts` → `src/infrastructure/external/openalex.py` + `POST /citations/refresh` (owner-only, пагинация limit/offset).

**Доступ:** публичные read-эндпоинты открыты аноним; owner-only (`/citations/external`, `/citations/refresh`, `/admin/editor-options`) и кабинет (`/researcher/me/*`) — через `require_owner`/`get_current_profile` (сервисы дополнительно требуют `caller_is_owner`, защита в глубину как у SECURITY DEFINER).

**Приёмка:** ✅ `tests/verify_phase5.py` — **52/52** против реальной БД (семантика каждого RPC: батч-статы, journal/platform/analytics/top/daily, view-дедуп по IP + like↔dislike toggle, internal/external/`cited_by`, citing-граф, DOI-нормализация и матчинг, claim идемпотентность + no-ORCID→ошибка, import full-replace, editor-options фильтр по роли), фикстуры удаляются за собой (0 остатков). ✅ ASGI-smoke: публичные `/citations/*`, `/stats/*` → 200; `/citations/external`, `/citations/refresh`, `/admin/editor-options`, `/researcher/me/*` аноним → 401.
**Оценка:** 1.5–2 недели. **Риск:** средний. **Факт:** сделано.

> **Осталось (на Фазу 8/сверку с прод-БД):** 7 stats-RPC (`get_journal_stats`/`get_platform_stats`/`get_journal_analytics`/`get_top_articles`/`get_daily_stats`/`add_interaction`/`increment_article_views`) жили только в проде, не в `supabase/*.sql` — их семантика восстановлена по вызовам во фронте (`useAnalytics.ts`, `StatisticsSection`, `JournalsSwiper`, journal page); при cutover сверить топ-N/итоги с текущим ответом Supabase-RPC на тех же входах. `OPENALEX_MAILTO` — в `.env` (по умолчанию `info@researcher.uz`).

---

## Фаза 6 — Storage (Supabase Storage → Cloudflare R2) 🟡 КОД ГОТОВ (ждёт R2-кредов)
**Цель:** заменить Supabase Storage на **Cloudflare R2** (решение владельца). Бакеты Supabase (`pdfs`, `cover`, `avatars`) → единый R2-бакет как префиксы ключей: `pdfs/<file>`, `cover/<file>`, `avatars/<file>` (имена уникальны — timestamped-слаги).

**URL-стратегия (решение владельца):** PDF остаются за бэкенд-прокси `/pdf/<slug>` (Scholar требует тот же домен) — поля `articles.pdf`/`issues.full_pdf` НЕ переписываем, прокси извлекает ключ из сохранённого URL. Обложки/логотипы/аватары переписываются на публичные R2-URL для прямой раздачи (`<img src>`).

- [x] Клиент `boto3` (S3 API) — `src/infrastructure/storage.py`: ленивый R2-клиент (без кредов → `StorageNotConfigured` → 503), `put`/`get`/`delete`/`exists`, `key_from_url` (Supabase/R2/голое имя → `<prefix>/<file>`), `public_url`. Env: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_PUBLIC_BASE_URL` (+ `R2_ENDPOINT` — property). `boto3`/`python-multipart` в requirements.
- [x] Прокси-раздача PDF — `src/api/v1/files.py` `GET /pdf/{filename}`: порт `app/pdf/[filename]/route.ts`, сохранены защита `Content-Disposition` (регексп `[^a-zA-Z0-9._-]`), ETag, кэш-заголовки, 304/404/502/503. Раздаёт из R2 (`storage.get` через `run_in_threadpool`).
- [x] Загрузка/удаление из админки — `src/api/v1/uploads.py` (auth): `POST /storage/upload` (multipart, prefix ∈ pdfs/cover/avatars, лимит 60 МБ, timestamped-ключ, возвращает `{key,url}`), `DELETE /storage/by-url` (порт `removeStorageFileByUrl`). Порт `src/lib/storageUpload.ts`.
- [x] Скрипт переноса — `scripts/migrate_storage_to_r2.py`: `copy` (Supabase Storage list+download → R2 put, идемпотентно по `head_object`), `verify` (число объектов Supabase vs R2 по префиксу), `rewrite-urls [--dry-run]` (image-колонки `profiles.avatar_url`/`publishers.logo`/`journals.cover_image|logo`/`issues.cover_image`/`articles.cover_image` → R2 public-URL; PDF-поля не трогает).
- [ ] **Внешнее (владелец):** создать R2-бакет + API-токен, включить публичную раздачу (r2.dev или кастомный домен) → заполнить 5 `R2_*` в `.env`. Затем: `copy` → `verify` (счёт совпал) → `rewrite-urls`.

**Приёмка (после кредов):** старая PDF-ссылка открывается через `/pdf/<slug>` из R2; новая загрузка из админки уходит в R2 end-to-end; `verify` — счёт объектов совпал; картинки грузятся с R2-URL.
**Оценка:** 3–5 дней. **Риск:** средний (объём файлов). **Факт:** код готов и проверен (импорт/маршруты/гейтинг: `/pdf` публичный 404-на-отсутствии, `/storage/*` аноним → 401, скрипт компилируется, R2-guard срабатывает без кредов); ждёт R2-кредов для прогона переноса.

---

## Фаза 7 — Поиск и embeddings 🟡 ПОЛНОТЕКСТ ГОТОВ (семантика ждёт модель)
**Цель:** полнотекст (5 языков) + семантика.

- [x] Пересборка `tsvector` — `scripts/build_search_index.py` (прогнан на live БД): функция `article_search_tsv(...)` + триггер `articles_tsv_update` (before insert/update нужных колонок) + начальный populate. **Все 1937 статей проиндексированы** (`search_vector` был пуст — Phase 2 его не переносила). Контент мультиязычный (узб. латиница + рус + англ), узбекского стеммера в PG нет → основной `search_vector` на `simple` + `unaccent` с весами (заголовок A, ключевые/авторы B, аннотация C); `document_ru/en/uz` — russian/english/simple для полноты.
- [x] `search_articles` — `src/domain/search.py` + `GET /search?q=`: `websearch_to_tsquery('simple', unaccent(q))` + `ts_rank`, при пустом FTS — триграммный fallback (`<%` word_similarity, ловит опечатки). Порядок `rank desc, data desc`; дату отдаём как `data` + `created_at` (фронт мапит); views/downloads доклеиваются батчем (как `enrichedData` во фронте). Лимит 100 (фронт пагинирует клиентски; серверная пагинация — в Фазе 8).
- [x] Индексы: GIN на `search_vector` + `document_ru/en/uz`, GIN trgm на `title`.
- [x] `pgvector` — семантический поиск `SearchDomain.search_by_vector` (косинус `<=>`): **1316/1937 статей уже несут 768-мерный эмбеддинг** (перенесены из прода). Self-query (эмбеддинг статьи → она сама top-1, score 1.0) работает; выдаёт семантически близкие.
- [ ] **Осталось (владелец):** модель эмбеддингов запроса. Хранимые векторы 768-мерные, но модель-источник в репозитории отсутствует — чтобы векторизовать пользовательский запрос той же моделью, нужно подтвердить, чем генерировались эмбеддинги (напр. multilingual-e5-base / paraphrase-multilingual-mpnet — обе 768). До этого `search_by_vector` работает только с уже готовым вектором. Опц.: после подтверждения — `ALTER COLUMN embedding TYPE vector(768)` + hnsw-индекс `vector_cosine_ops` (сейчас брутфорс по 1316 строкам < 10 мс, индекс не критичен).

**Приёмка:** ✅ контрольные запросы (`bolalar`/`ta'lim`/`integratsiya` → 100, `rinolaliya` → 2 точных, опечатка `rinolaliyya` → находит через fallback, `''`/мусор → 0); ASGI `GET /search` → 200; vector self-query → сам объект top-1. Полное совпадение топ-N с прод-RPC `search_articles` — сверить при cutover (RPC был только в проде, семантика восстановлена по `SearchPageClient.tsx`).
**Оценка:** 1 неделя. **Риск:** средний. **Факт:** полнотекст сделан и проверен на живой БД; семантика — инфраструктура готова, ждёт модель.

---

> **Платежи (Multicard) исключены из миграции** — по решению владельца сейчас всё равно не работают. `api/payment/*`, `card-callback`, `webhook` и `MULTICARD_SECRET` не переносятся; при cutover фронта соответствующие вызовы удаляются, а не портируются. Если платежи понадобятся позже — отдельный проект поверх нового бэкенда.

---

## Фаза 8 — Переключение фронтенда
**Цель:** 81 файл Next.js → новый API вместо Supabase SDK.

Инвентаризация показала, что переключать было некуда: фронт читает из таблиц,
по которым не было ни одного маршрута. Поэтому фаза разделена на 8a (добор
эндпоинтов) и 8b (собственно переключение).

### 8a — недостающие ресурсы бэкенда ✅ ГОТОВО
Покрытие по обращениям фронта: `issues` (21), `publishers` (14),
`conference_sections` (7), `article_authors` (5), `saved_articles` (4),
`article_references` (4). Маршрутов стало 48 → 65.

- [x] `/issues` — список (+`article_count` коррелированным подзапросом, чтобы
      пустые выпуски не исчезали), `/issues/years`, CRUD.
- [x] `/publishers` — список/`mine`/по slug/по id, CRUD; `admin_id`
      переназначает только owner.
- [x] `/conference-sections` — CRUD + `/reorder` (порядок одной транзакцией
      вместо двух несогласованных UPDATE'ов на фронте).
- [x] `/articles/{id}/authors` и `/articles/{id}/references` — чтение + полная
      замена; `/articles/references/citing` — батч для CitationsDashboard.
- [x] `/researcher/{orcid}/publications` — статьи платформы по ORCID автора
      (в отличие от `works`, импортированных из ORCID).
- [x] `/library` — сохранённые статьи (идемпотентное сохранение, счётчики
      просмотров/скачиваний, `embedding` наружу не отдаётся).
- [x] Удаление контейнера = **откреп, а не каскад**: удаление выпуска/издателя/
      секции сохраняет статьи, обнуляя ссылку (секции тома удаляются — их FK
      `NOT NULL`, осиротеть не могут).

**Приёмка:** ✅ `tests/verify_phase8a.py` — **54/54** против реальной БД
(порядок сортировок, счётчики, `exclude_unset` в update, идемпотентность,
reorder с мусорными id, откреп при удалении), фикстуры удаляются за собой
(0 остатков). ✅ ASGI-smoke: публичное чтение → 200, все записи анонимом → 401.

### 8b — переключение фронтенда
- [ ] Ввести `apiClient` (fetch-обёртка к FastAPI) + типы ответов.
- [ ] Переключать по доменам в порядке риска:
  1. Публичные read-only (журналы, статьи, конференции, паблишеры, sitemap, scholar).
  2. Статистика/поиск.
  3. Профили/кабинет исследователя.
  4. Админка (журналы, публикации, паблишеры, owner-консоль).
  5. **Auth + сессии** (переписать middleware-гейтинг под новые cookie/JWT).
- [ ] Удалить `@supabase/*` из `package.json` и 4-уровневую клиентскую модель — в самом конце.
- [ ] SEO-инварианты сохранить: канонический локаль `uz` для статей/журналов/паблишеров (см. tech-debt в CLAUDE.md).

**Приёмка:** страница за страницей — визуальный + функциональный паритет; в конце `grep @supabase src` = 0.
**Оценка:** 2–3 недели. **Риск:** высокий (объём + middleware/SSR-сессии).

---

## Фаза 9 — Cutover и эксплуатация
- [ ] Freeze записи в Supabase → финальный `pg_dump` дельты → restore.
- [ ] Переключение DNS/окружения фронта на прод-URL API.
- [ ] План отката (Supabase держим в read-only ≥2 недели).
- [ ] Мониторинг/логи/бэкапы своего Postgres (раньше это делал Supabase).
- [ ] CI: тесты бэкенда + сохранить security-тесты фронта (csrf, xss, pdf-proxy).

---

## Сводная оценка и порядок

| Фаза | Оценка | Риск | Блокирует |
|---|---|---|---|
| 1. Модель данных | 2–3 дня | низкий | всё |
| 2. Миграция данных | 1–2 дня | средний | 3–9 |
| 3. Auth (Google OAuth + пароли) | 1.5–2 нед | **высокий** | 4, 8.5 |
| 4. Авторизация | 4–6 дней | **высокий** | админ-эндпоинты |
| 5. RPC-сервисы | 1.5–2 нед | средний | 8 |
| 6. Storage | 3–5 дней | средний | PDF-страницы |
| 7. Поиск/embeddings | 1 нед | средний | поиск на фронте |
| 8. Фронт-cutover | 2–3 нед | **высокий** | запуск |
| 9. Cutover/эксплуатация | 2–3 дня | высокий | — |

**Грубый итог: ~7–9 недель** одним разработчиком (без платежей; Google OAuth в Auth добавил ~полнедели). Фазы 1–2 разблокируют параллельную работу; 3 (Auth) и 8 (фронт-cutover) — критические по риску.

### Ближайшие 3 шага
1. Проверить доступ к `auth.users.encrypted_password` (путь Фазы 3) и выбрать целевое хранилище (Фаза 6). См. «Открытые вопросы».
2. Фаза 1 — синхронизировать модели со `schema.sql` (могу начать сразу).
3. Фаза 2 — пробный `pg_dump` на копии, свериться по строкам.

### Открытые вопросы
- **Пароли/Auth:** ✅ решено. 86 юзеров = 80 Google OAuth + 9 email/password. Стратегия: **Google OAuth как основной вход** + перенос 9 bcrypt-хешей (вариант A, без ресета). Требует настройки OAuth-приложения в Google Cloud Console.
- **Storage (Фаза 6):** ✅ выбран **Cloudflare R2** (S3-совместимо, egress бесплатный). Перенос из бакета `pdfs` по S3-протоколу.
- Целевой хостинг backend + Postgres (VPS, Railway, Fly, свой сервер)?
- `article_authors` и `article_interactions` не имели RLS — при переносе закрыть проверками (Фаза 4).

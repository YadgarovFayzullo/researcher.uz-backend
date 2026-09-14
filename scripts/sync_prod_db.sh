#!/usr/bin/env bash
# Синхронизация прод-базы в локальную — по расписанию, но только когда есть что
# синхронизировать.
#
# Зачем «только когда». Полный дамп прода — под сотню мегабайт и несколько
# минут с остановкой локального api; гонять его каждые два часа впустую нет
# смысла. Поэтому сначала снимается дешёвый отпечаток (число статей, выпусков,
# журналов и время последней правки статьи), и работа начинается, только если
# он разошёлся с локальным.
#
# ВНИМАНИЕ: синк ОДНОСТОРОННИЙ и разрушительный для локальной базы — она
# полностью пересоздаётся из прода. Всё, что вы навводили локально, исчезнет.
#
# Грабли, заложенные в код:
#   * расширения vector и pg_trgm создаются в свежей базе ДО pg_restore,
#     иначе восстановление падает на типах колонок;
#   * локальный api останавливается на время работы — иначе держит коннекты и
#     `drop database` не проходит;
#   * на сервере compose требует `-f docker-compose.prod.yml` в КАЖДОЙ команде:
#     рядом лежит dev-файл с другим томом и паролем.
#
# Ручной запуск:  bash scripts/sync_prod_db.sh          # синк при изменениях
#                 bash scripts/sync_prod_db.sh --force  # синк всегда
#                 bash scripts/sync_prod_db.sh --check  # только сравнить
set -uo pipefail

# Прод переехал на DigitalOcean 2026-09-10: заходим root-ом (не ubuntu, как на
# старом AWS), стек лежит в /root/app, sudo не нужен.
PROD_SSH="${PROD_SSH:-root@159.223.153.155}"
PROD_APP_DIR="${PROD_APP_DIR:-/root/app}"
PROD_COMPOSE="docker compose -f docker-compose.prod.yml"
DB_NAME="${DB_NAME:-scientific_db}"
LOCAL_DB="${LOCAL_DB:-researcheruz-backend-db-1}"
LOCAL_API="${LOCAL_API:-researcheruz-backend-api-1}"
DUMP="${TMPDIR:-/tmp}/researcheruz-prod.pgc"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FINGERPRINT_SQL="select (select count(*) from articles)||'/'||(select count(*) from issues)||'/'||(select count(*) from journals)||'/'||coalesce((select max(updated_at)::text from articles),'-')"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# Контейнер локальной базы остаётся остановленным после перезапуска Docker
# Desktop или перезагрузки мака, и раньше скрипт этого не замечал: все
# `docker exec` молча падали, отпечаток выходил пустым («нет базы»), дамп на
# 96 МБ качался впустую 14 минут, а `pg_restore` не проходил вовсе — локальная
# база застывала на старом состоянии, и синк тихо не работал сутками.
ensure_local_db_up() {
  docker exec "$LOCAL_DB" pg_isready -U postgres -q >/dev/null 2>&1 && return 0
  log "локальный контейнер базы не запущен — поднимаем..."
  docker start "$LOCAL_DB" >/dev/null 2>&1 ||
    (cd "$REPO_DIR" && docker compose up -d db >/dev/null 2>&1)
  # Postgres принимает коннекты не сразу после старта контейнера.
  for _ in $(seq 1 30); do
    docker exec "$LOCAL_DB" pg_isready -U postgres -q >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

mode="${1:-}"

# Запуск по расписанию и ручной запуск могут совпасть: launchd не пускает две
# копии ОДНОЙ задачи, но от `bash scripts/sync_prod_db.sh` руками это не
# защищает, а два одновременных `drop database` оставили бы локальную базу
# полупустой. Лок — каталогом: mkdir атомарен, в отличие от проверки файла.
LOCK_DIR="${TMPDIR:-/tmp}/researcheruz-db-sync.lock"
acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ >"$LOCK_DIR/pid"
    trap 'rm -rf "$LOCK_DIR"' EXIT
    return 0
  fi
  # Лок мог остаться от процесса, убитого на полпути (перезагрузка во время
  # синка) — тогда он держал бы синк навсегда. Живость проверяем по pid.
  local holder
  holder=$(cat "$LOCK_DIR/pid" 2>/dev/null)
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    return 1
  fi
  log "снимаем зависший лок от процесса ${holder:-?}"
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR" 2>/dev/null || return 1
  echo $$ >"$LOCK_DIR/pid"
  trap 'rm -rf "$LOCK_DIR"' EXIT
  return 0
}

# --check ничего не меняет, ему лок не нужен.
if [ "$mode" != "--check" ] && ! acquire_lock; then
  log "синк уже идёт (pid $(cat "$LOCK_DIR/pid" 2>/dev/null)) — выходим"
  exit 0
fi

# --- отпечатки ---------------------------------------------------------------
# Пропущенные во сне срабатывания launchd выполняет сразу после пробуждения, а
# Wi-Fi в этот момент ещё не поднялся: единственная попытка давала «прод
# недоступен», и синк отодвигался на два часа. Поэтому три подхода.
prod_fp=""
for attempt in 1 2 3; do
  prod_fp=$(ssh -o ConnectTimeout=20 -o BatchMode=yes "$PROD_SSH" \
    "cd $PROD_APP_DIR && $PROD_COMPOSE exec -T db psql -U postgres -d $DB_NAME -Atc \"$FINGERPRINT_SQL\"" 2>/dev/null | tr -d '\r')
  [ -n "$prod_fp" ] && break
  if [ "$attempt" -lt 3 ]; then
    log "прод не ответил (попытка $attempt из 3) — ждём 30 с"
    sleep 30
  fi
done
if [ -z "$prod_fp" ]; then
  log "прод недоступен или запрос не прошёл — выходим, локальную базу не трогаем"
  exit 1
fi

if ! ensure_local_db_up; then
  log "локальный контейнер базы не поднялся — выходим, дамп не качаем"
  exit 1
fi

local_fp=$(docker exec "$LOCAL_DB" psql -U postgres -d "$DB_NAME" -Atc "$FINGERPRINT_SQL" 2>/dev/null | tr -d '\r')

log "прод:   $prod_fp"
log "локаль: ${local_fp:-<нет базы>}"

if [ "$mode" = "--check" ]; then
  [ "$prod_fp" = "$local_fp" ] && log "изменений нет" || log "есть изменения"
  exit 0
fi

if [ "$prod_fp" = "$local_fp" ] && [ "$mode" != "--force" ]; then
  log "изменений нет — синк не нужен"
  exit 0
fi

# --- дамп --------------------------------------------------------------------
log "снимаем дамп прода..."
# Стримом через ssh, без промежуточного файла на сервере: у него мало места, и
# оставлять там копию базы незачем.
if ! ssh -o ConnectTimeout=20 "$PROD_SSH" \
    "cd $PROD_APP_DIR && $PROD_COMPOSE exec -T db pg_dump -U postgres -Fc --no-owner --no-acl $DB_NAME" > "$DUMP" 2>/dev/null; then
  log "дамп не снялся — локальная база НЕ тронута"
  exit 1
fi
size=$(du -h "$DUMP" | cut -f1)
# Пустой или обрезанный дамп восстанавливать нельзя — это молча снесло бы
# локальную базу и оставило её пустой.
if [ ! -s "$DUMP" ] || [ "$(stat -f%z "$DUMP" 2>/dev/null || stat -c%s "$DUMP")" -lt 1000000 ]; then
  log "дамп подозрительно мал ($size) — прерываем, локальная база НЕ тронута"
  exit 1
fi
log "дамп получен: $size"

# --- восстановление ----------------------------------------------------------
log "останавливаем локальный api..."
docker stop "$LOCAL_API" >/dev/null 2>&1

log "пересоздаём базу..."
docker exec "$LOCAL_DB" psql -U postgres -d postgres -c \
  "select pg_terminate_backend(pid) from pg_stat_activity where datname='$DB_NAME' and pid<>pg_backend_pid()" >/dev/null 2>&1
docker exec "$LOCAL_DB" psql -U postgres -d postgres -c "drop database if exists $DB_NAME" >/dev/null
docker exec "$LOCAL_DB" psql -U postgres -d postgres -c "create database $DB_NAME" >/dev/null
docker exec "$LOCAL_DB" psql -U postgres -d "$DB_NAME" -c "create extension if not exists vector" -c "create extension if not exists pg_trgm" >/dev/null

log "восстанавливаем..."
# pg_restore многословен на предупреждениях о правах и extension-ах — это
# нормально, поэтому смотрим не на вывод, а на код возврата и итоговые цифры.
docker exec -i "$LOCAL_DB" pg_restore -U postgres -d "$DB_NAME" --no-owner --no-acl < "$DUMP" >/dev/null 2>&1
restored=$(docker exec "$LOCAL_DB" psql -U postgres -d "$DB_NAME" -Atc "$FINGERPRINT_SQL" 2>/dev/null | tr -d '\r')

log "поднимаем локальный api..."
docker start "$LOCAL_API" >/dev/null 2>&1

if [ "$restored" = "$prod_fp" ]; then
  log "готово: локальная база = проду ($restored)"
  exit 0
fi
log "ВНИМАНИЕ: после восстановления отпечаток разошёлся (стало $restored, ждали $prod_fp)"
exit 1

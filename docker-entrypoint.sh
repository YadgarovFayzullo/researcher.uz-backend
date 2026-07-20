#!/bin/sh
set -e

# Миграции на старте — опционально. RUN_MIGRATIONS=1 удобно для одиночного
# инстанса. При нескольких репликах так делать НЕ стоит (гонка одновременных
# upgrade): гоняйте `alembic upgrade head` отдельной разовой задачей при деплое,
# а RUN_MIGRATIONS оставьте пустым. Alembic берёт DATABASE_URL из настроек.
if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  echo "[entrypoint] alembic upgrade head"
  alembic upgrade head
fi

# WEB_CONCURRENCY — число воркеров (по умолчанию 2). PORT переопределяем при
# необходимости платформой. exec — чтобы uvicorn стал PID 1 и ловил сигналы.
exec uvicorn src.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}"

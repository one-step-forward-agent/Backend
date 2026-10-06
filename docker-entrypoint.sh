#!/bin/sh
set -e

storage="${STORAGE_PATH:-/app/storage}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$storage"
    find "$storage" ! -user app -exec chown app:app {} +
    exec gosu app "$0" "$@"
fi

alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers

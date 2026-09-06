#!/bin/sh
# dev-запуск API на :8040 со скретч-юзерами (браузерная проверка ПЕРВИЧКИ)
export AUTH_USERS_FILE="${AUTH_USERS_FILE:?}"
export BARS_WORKER="${BARS_WORKER:-0}"
exec .venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8040

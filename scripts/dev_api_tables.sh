#!/bin/sh
# dev-запуск API на :8021 со скретч-юзерами (браузерная проверка выравнивания таблиц)
export AUTH_USERS_FILE="${AUTH_USERS_FILE:-/private/tmp/claude-501/-Users-ishabaev-python-projects-shabshab/d871a374-825e-4cf7-9b96-994c15136198/scratchpad/users.json}"
# часовой налив баров/тиков по всему юниверсу в dev не нужен
export BARS_WORKER="${BARS_WORKER:-0}"
exec .venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8021

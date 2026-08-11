#!/bin/sh
# dev-запуск API на :8040 со скретч-юзерами (браузерная проверка вкладки КРУПНЫЕ)
export AUTH_USERS_FILE="${AUTH_USERS_FILE:-/private/tmp/claude-501/-Users-ishabaev-python-projects-shabshab/403d711c-72d2-4fa6-99d0-1687a2b2ace7/scratchpad/users.json}"
# часовой налив баров/тиков по всему юниверсу в dev не нужен
export BARS_WORKER="${BARS_WORKER:-0}"
exec .venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8040

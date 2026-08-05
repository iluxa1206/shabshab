#!/bin/sh
# dev-запуск API на :8030 со скретч-юзерами (браузерная проверка вкладки СДЕЛКИ)
export AUTH_USERS_FILE="${AUTH_USERS_FILE:-/private/tmp/claude-501/-Users-ishabaev-python-projects-shabshab/580c07e9-dda7-4d19-b7ac-5d1f63cdf396/scratchpad/users.json}"
# часовой налив баров/тиков по всему юниверсу в dev не нужен — включается точечно
export BARS_WORKER="${BARS_WORKER:-0}"
exec .venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8030

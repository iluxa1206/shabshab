#!/bin/sh
# dev-запуск API на :8020 со скретч-юзерами (для браузерной проверки из Claude-сессии)
export AUTH_USERS_FILE="/private/tmp/claude-501/-Users-ishabaev-python-projects-shabshab/dcaa78c2-e733-4e06-a3df-51e043dcf376/scratchpad/users.json"
exec .venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8020

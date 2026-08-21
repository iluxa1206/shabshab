#!/usr/bin/env bash
# Редеплой Floaters Desk на assetallocator.ru/desk (VPS 161.104.17.23, стек floaters-prod).
# Прод-URL мигрировал 2026-07-21: deskdeskdesk.ru → assetallocator.ru/desk/ (Caddy path-route).
# Инфра: контейнер floaters-prod в docker-сети astra-prod_default, за Caddy (TLS Let's Encrypt).
#
# Использование: ./scripts/deploy.sh
#
# DEPLOY_SSH_BIND — локальный интерфейс или IP, с которого идти на сервер.
# Нужен, когда поднят VPN/прокси в режиме TUN: дефолтный маршрут уходит в
# туннель, DNS отдаёт fake-ip (198.18.x.x), и ssh/rsync/healthcheck молча
# таймаутят, хотя TCP-порт отвечает. Привязка к физическому интерфейсу проходит
# мимо туннеля, ничего не меняя в системных настройках:
#   DEPLOY_SSH_BIND=en0 ./scripts/deploy.sh          # имя интерфейса
#   DEPLOY_SSH_BIND=192.168.50.154 ./scripts/deploy.sh   # или сразу IP
set -euo pipefail

SERVER=root@161.104.17.23
REMOTE=/root/floaters
HEALTH_URL=https://assetallocator.ru/desk/api/health

cd "$(dirname "$0")/.."

BIND="${DEPLOY_SSH_BIND:-}"
SSH_OPTS="-o BatchMode=yes"
CURL_OPTS=()
if [ -n "$BIND" ]; then
  # имя интерфейса (en0) → его IPv4; если передан уже IP, берём как есть
  BIND_IP="$BIND"
  if command -v ipconfig >/dev/null 2>&1; then
    RESOLVED=$(ipconfig getifaddr "$BIND" 2>/dev/null || true)
    [ -n "$RESOLVED" ] && BIND_IP="$RESOLVED"
  fi
  echo ">>> обход туннеля: локальный адрес $BIND_IP"
  SSH_OPTS="-b $BIND_IP $SSH_OPTS"
  CURL_OPTS=(--interface "$BIND_IP")
fi

echo ">>> rsync → $SERVER:$REMOTE"
rsync -az --delete \
  --exclude '.git' \
  --exclude '.env' \
  --exclude 'data' \
  --exclude '.venv' \
  --exclude 'frontend-react/node_modules' \
  --exclude 'frontend-react/dist' \
  --exclude '__pycache__' \
  --exclude '**/__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.claude' \
  --exclude '*.xlsm' \
  --exclude '*.pyc' \
  -e "ssh $SSH_OPTS" \
  ./ "$SERVER:$REMOTE/"

echo ">>> docker compose up --build"
# shellcheck disable=SC2086
ssh $SSH_OPTS "$SERVER" \
  "cd $REMOTE && docker compose -f docker-compose.prod.yml --env-file .env up -d --build"

# Крон резервных копий: ставим/обновляем при каждом деплое — расписание должно
# ехать вместе с кодом, а не жить только в голове того, кто его однажды завёл.
# 04:30 МСК: после ночного прун/VACUUM/ANALYZE в 03:30 (и после прогрева спреда
# в 03:00) — бэкап снимается с уже сжатой базы.
# СЕРВЕР ЖИВЁТ В МСК, не в UTC: `date` на 161.104.17.23 отдаёт MSK, и cron берёт
# ту же зону. Прежний комментарий обещал пересчёт «UTC → 01:30», из-за чего
# расписание стояло на 30 1 и бэкап уходил в 01:30 МСК — ДО прунов, то есть
# ровно мимо замысла. Часы в cron ниже — московские, пересчитывать не надо.
echo ">>> cron бэкапов"
# shellcheck disable=SC2086
ssh $SSH_OPTS "$SERVER" "cat > /etc/cron.d/floaters-backup <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
30 4 * * * root cd $REMOTE && docker compose -f docker-compose.prod.yml exec -T floaters python scripts/backup_db.py >> $REMOTE/data/logs/backup.log 2>&1
CRON
chmod 0644 /etc/cron.d/floaters-backup"

echo ">>> healthcheck"
# ${CURL_OPTS[@]} на пустом массиве под set -u — «unbound variable» в bash 3.2
# (штатный /bin/bash macOS): деплой падал НА ПОСЛЕДНЕЙ строке, когда
# DEPLOY_SSH_BIND не задан, хотя прод уже поднялся. Разворачиваем через ${x+"${x[@]}"}.
# Первый ответ после рестарта идёт ~7с (холодные кэши) — таймаут с запасом.
curl -s --max-time 30 ${CURL_OPTS[@]+"${CURL_OPTS[@]}"} "$HEALTH_URL"
echo
echo ">>> done: https://assetallocator.ru/desk/app/"

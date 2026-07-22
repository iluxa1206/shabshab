#!/usr/bin/env bash
# Редеплой Floaters Desk на assetallocator.ru/desk (VPS 161.104.17.23, стек floaters-prod).
# Прод-URL мигрировал 2026-07-21: deskdeskdesk.ru → assetallocator.ru/desk/ (Caddy path-route).
# Инфра: контейнер floaters-prod в docker-сети astra-prod_default, за Caddy (TLS Let's Encrypt).
# Использование: ./scripts/deploy.sh
set -euo pipefail

SERVER=root@161.104.17.23
REMOTE=/root/floaters

cd "$(dirname "$0")/.."

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
  -e 'ssh -o BatchMode=yes' \
  ./ "$SERVER:$REMOTE/"

echo ">>> docker compose up --build"
ssh -o BatchMode=yes "$SERVER" \
  "cd $REMOTE && docker compose -f docker-compose.prod.yml --env-file .env up -d --build"

echo ">>> healthcheck"
curl -s --max-time 15 https://assetallocator.ru/desk/api/health
echo
echo ">>> done: https://assetallocator.ru/desk/app/"

#!/usr/bin/env bash
# Стянуть свежую резервную копию прод-базы на эту машину.
#
# Копия на сервере спасает от «уронил таблицу» и кривой миграции, но НЕ от
# смерти VPS: тиковый архив живёт в одном экземпляре, у Alor глубина 30 дней.
# Поэтому раз в неделю копия должна уезжать наружу — этим скриптом.
#
# Использование:
#   ./scripts/pull_backup.sh                 # в ./backups рядом с репо
#   ./scripts/pull_backup.sh /Volumes/ssd    # в свой каталог
#   DEPLOY_SSH_BIND=en0 ./scripts/pull_backup.sh   # обход VPN-туннеля, как в deploy.sh
set -euo pipefail

SERVER=root@161.104.17.23
REMOTE=/root/floaters/data/backups
DEST="${1:-$(cd "$(dirname "$0")/.." && pwd)/backups}"

BIND="${DEPLOY_SSH_BIND:-}"
SSH_OPTS="-o BatchMode=yes"
if [ -n "$BIND" ]; then
  BIND_IP="$BIND"
  if command -v ipconfig >/dev/null 2>&1; then
    RESOLVED=$(ipconfig getifaddr "$BIND" 2>/dev/null || true)
    [ -n "$RESOLVED" ] && BIND_IP="$RESOLVED"
  fi
  SSH_OPTS="-b $BIND_IP $SSH_OPTS"
fi

mkdir -p "$DEST"
echo ">>> что лежит на сервере"
# shellcheck disable=SC2086
ssh $SSH_OPTS "$SERVER" "ls -lh $REMOTE"

echo ">>> тянем свежие копии в $DEST"
# shellcheck disable=SC2086
# без --progress: 500 МБ построчного прогресса топят полезный вывод
rsync -ah --stats -e "ssh $SSH_OPTS" \
  "$SERVER:$REMOTE/" "$DEST/"

echo ">>> проверка распаковкой (последняя копия portfolio)"
LAST=$(ls -t "$DEST"/portfolio-*.db.gz 2>/dev/null | head -1 || true)
if [ -n "$LAST" ]; then
  gzip -t "$LAST" && echo "архив цел: $(basename "$LAST")"
else
  echo "копий portfolio-*.db.gz не нашлось — проверь вывод выше"
fi

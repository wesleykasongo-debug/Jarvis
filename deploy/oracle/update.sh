#!/usr/bin/env bash
# Met à jour JARVIS sur la VM : récupère le dernier code et redémarre.
#   sudo bash /opt/jarvis/deploy/oracle/update.sh
set -euo pipefail

APP_DIR="/opt/jarvis"
[ "$(id -u)" -eq 0 ] || { echo "Lancez avec sudo." >&2; exit 1; }

git -C "$APP_DIR" pull --ff-only
docker compose -f "$APP_DIR/deploy/oracle/docker-compose.yml" up -d --build --remove-orphans
docker image prune -f >/dev/null

source "$APP_DIR/deploy/oracle/.env"
for _ in $(seq 1 30); do
  if curl -fsS --max-time 5 "https://$DOMAIN/health/ready"; then echo; echo "Mise à jour terminée."; exit 0; fi
  sleep 3
done
echo "L'API ne répond pas : docker compose -f $APP_DIR/deploy/oracle/docker-compose.yml logs --tail 50 api" >&2
exit 1

#!/usr/bin/env bash
# Installation de JARVIS sur une VM Ubuntu (Oracle Cloud Always Free).
#
# Sur la VM :
#   curl -fsSLO https://raw.githubusercontent.com/wesleykasongo-debug/Jarvis/main/deploy/oracle/install.sh
#   sudo bash install.sh
#
# Le script est ré-exécutable : il ne redemande pas ce qui est déjà configuré.
set -euo pipefail

REPO_URL="https://github.com/wesleykasongo-debug/Jarvis.git"
APP_DIR="/opt/jarvis"
COMPOSE=(docker compose -f "$APP_DIR/deploy/oracle/docker-compose.yml")

info()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn()  { printf '\033[1;33m%s\033[0m\n' "$*"; }
fail()  { printf '\033[1;31mERREUR : %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "lancez ce script avec sudo : sudo bash install.sh"
[ -r /dev/tty ] || fail "terminal interactif requis (connectez-vous en SSH)"

# Questions posées au clavier, même si le script est lancé via un pipe.
ask() {        # ask <variable> <question> [défaut]
  local __var=$1 __q=$2 __def=${3:-} __ans
  if [ -n "$__def" ]; then read -rp "  $__q [$__def] : " __ans </dev/tty; __ans=${__ans:-$__def}
  else while [ -z "${__ans:-}" ]; do read -rp "  $__q : " __ans </dev/tty; done; fi
  printf -v "$__var" '%s' "$__ans"
}
ask_secret() { # ask_secret <variable> <question>
  local __var=$1 __q=$2 __ans=""
  while [ -z "$__ans" ]; do read -rsp "  $__q (saisie masquée) : " __ans </dev/tty; echo; done
  printf -v "$__var" '%s' "$__ans"
}

# --- 1. Paquets système ---------------------------------------------------------
info "Mise à jour du système et outils de base"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates iptables-persistent netfilter-persistent >/dev/null

# --- 2. Docker ------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  info "Installation de Docker (script officiel get.docker.com)"
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null
[ -n "${SUDO_USER:-}" ] && usermod -aG docker "$SUDO_USER" || true

# --- 3. Pare-feu de la VM -----------------------------------------------------------
# Les images Ubuntu d'Oracle bloquent tout sauf SSH via iptables : on ouvre 80/443.
info "Ouverture des ports 80 et 443"
for port in 80 443; do
  iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null \
    || iptables -I INPUT 5 -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null     || iptables -I INPUT 1 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
done
iptables -C INPUT -p udp --dport 443 -j ACCEPT 2>/dev/null \
  || iptables -I INPUT 5 -p udp --dport 443 -j ACCEPT 2>/dev/null   || iptables -I INPUT 1 -p udp --dport 443 -j ACCEPT
netfilter-persistent save >/dev/null

# --- 4. Swap (petites VM de 1 Go) -------------------------------------------------
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$mem_mb" -lt 2048 ] && ! swapon --show | grep -q .; then
  info "Mémoire faible ($mem_mb Mo) : création de 2 Go de swap"
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# --- 5. Code ------------------------------------------------------------------------
if [ -d "$APP_DIR/.git" ]; then
  info "Mise à jour du code ($APP_DIR)"
  git -C "$APP_DIR" pull --ff-only
else
  info "Téléchargement du code dans $APP_DIR"
  git clone --depth 1 "$REPO_URL" "$APP_DIR"
fi

# --- 6. Configuration -------------------------------------------------------------
BACKEND_ENV="$APP_DIR/backend/.env"
DEPLOY_ENV="$APP_DIR/deploy/oracle/.env"

if [ ! -f "$DEPLOY_ENV" ]; then
  info "Nom de domaine"
  public_ip=$(curl -fsS --max-time 5 https://api.ipify.org || true)
  default_domain=""
  [ -n "$public_ip" ] && default_domain="${public_ip//./-}.sslip.io"
  echo "  Sans nom de domaine, utilisez l'adresse gratuite proposée (sslip.io pointe vers l'IP de la VM)."
  ask DOMAIN "Domaine" "$default_domain"
  printf 'DOMAIN=%s\n' "$DOMAIN" > "$DEPLOY_ENV"
fi
# shellcheck disable=SC1090
source "$DEPLOY_ENV"

if [ ! -f "$BACKEND_ENV" ]; then
  info "Clés de l'API (copiées depuis backend/.env de votre PC)"
  ask        SUPABASE_URL              "SUPABASE_URL (https://<ref>.supabase.co)"
  ask_secret SUPABASE_SERVICE_ROLE_KEY "SUPABASE_SERVICE_ROLE_KEY (sb_secret_...)"
  ask        SUPABASE_PUBLISHABLE_KEY  "SUPABASE_PUBLISHABLE_KEY (sb_publishable_...)"
  ask_secret OPENAI_API_KEY            "OPENAI_API_KEY (sk-...)"
  ask        ENABLE_CONSOLE            "Activer la console de test /console (true/false)" "true"
  umask 077
  cat > "$BACKEND_ENV" <<EOF
SUPABASE_URL=${SUPABASE_URL%/}
SUPABASE_SERVICE_ROLE_KEY=$SUPABASE_SERVICE_ROLE_KEY
SUPABASE_PUBLISHABLE_KEY=$SUPABASE_PUBLISHABLE_KEY
SUPABASE_JWT_SECRET=
OPENAI_API_KEY=$OPENAI_API_KEY
OPENAI_MODEL=gpt-4o-mini
REDIS_URL=redis://redis:6379/0
CORS_ORIGINS=["https://$DOMAIN","http://localhost:3000"]
ENABLE_CONSOLE=$ENABLE_CONSOLE
LOG_LEVEL=INFO
EOF
  umask 022
fi
chmod 600 "$BACKEND_ENV"

# --- 7. Démarrage -----------------------------------------------------------------
info "Construction et démarrage (première fois : 3 à 5 minutes)"
"${COMPOSE[@]}" up -d --build
docker image prune -f >/dev/null

info "Attente de l'API et du certificat HTTPS"
ok=""
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 "https://$DOMAIN/health/ready" >/dev/null 2>&1; then ok=1; break; fi
  sleep 5
done

echo
if [ -n "$ok" ]; then
  printf '\033[1;32mJARVIS est en ligne 24h/24.\033[0m\n'
else
  warn "L'API ne répond pas encore en HTTPS. Vérifiez :"
  warn "  - les règles d'entrée 80/443 dans Oracle (Security List du sous-réseau) ;"
  warn "  - que le domaine pointe bien vers l'IP publique de la VM ;"
  warn "  - les logs : sudo docker compose -f $APP_DIR/deploy/oracle/docker-compose.yml logs --tail 50"
fi
cat <<EOF

  API            : https://$DOMAIN
  Console test   : https://$DOMAIN/console
  État           : https://$DOMAIN/health/ready

  Mettre à jour  : sudo bash $APP_DIR/deploy/oracle/update.sh
  Logs           : sudo docker compose -f $APP_DIR/deploy/oracle/docker-compose.yml logs -f api
  Modifier les clés : sudo nano $BACKEND_ENV  puis update.sh

  Supabase -> Authentication -> URL Configuration -> Site URL : https://$DOMAIN/console
EOF

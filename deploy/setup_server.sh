#!/usr/bin/env bash
set -euo pipefail

# ====== CONFIG ======
DOMAIN="biychat.uz"
EMAIL="dcbaalpvlape@gmail.com"
REPO_URL="git@github.com:SultanbekKenesbaev/ppp.git"
GIT_TOKEN="${GIT_TOKEN:-}"
SSH_KEY_PATH="${SSH_KEY_PATH:-/root/.ssh/id_ed25519}"
APP_DIR="/var/www/task-platform03"
APP_USER="www-data"
APP_GROUP="www-data"

# ====== HELPERS ======
log() { echo -e "\n[taskplatform] $*\n"; }

if [[ "$REPO_URL" == "CHANGE_ME_GIT_REPO_URL" ]]; then
  echo "ERROR: Set REPO_URL to your git repository URL in deploy/setup_server.sh"
  exit 1
fi

AUTH_REPO_URL="$REPO_URL"
if [[ -n "$GIT_TOKEN" && "$REPO_URL" == https://* ]]; then
  AUTH_REPO_URL="${REPO_URL/https:\/\//https:\/\/$GIT_TOKEN@}"
fi

GIT_SSH_CMD=""
if [[ "$REPO_URL" == git@* ]]; then
  if [[ ! -f "$SSH_KEY_PATH" ]]; then
    echo "ERROR: SSH key not found at $SSH_KEY_PATH"
    echo "Set SSH_KEY_PATH or place your private key there."
    exit 1
  fi
  GIT_SSH_CMD="ssh -i $SSH_KEY_PATH -o StrictHostKeyChecking=accept-new"
fi

# ====== PACKAGES ======
log "Installing system пакеты"
apt update
apt install -y python3-venv python3-pip nginx git certbot python3-certbot-nginx openssl

# ====== APP DIR ======
log "Preparing app directory"
mkdir -p "$APP_DIR"
chown "$APP_USER":"$APP_GROUP" "$APP_DIR"

# ====== CLONE / UPDATE ======
if [[ ! -d "$APP_DIR/.git" ]]; then
  log "Cloning repository"
  if [[ -n "$GIT_SSH_CMD" ]]; then
    GIT_SSH_COMMAND="$GIT_SSH_CMD" git clone "$AUTH_REPO_URL" "$APP_DIR"
  else
    git clone "$AUTH_REPO_URL" "$APP_DIR"
  fi
  chown -R "$APP_USER":"$APP_GROUP" "$APP_DIR"
else
  log "Updating repository"
  git -C "$APP_DIR" remote set-url origin "$AUTH_REPO_URL"
  if [[ -n "$GIT_SSH_CMD" ]]; then
    GIT_SSH_COMMAND="$GIT_SSH_CMD" git -C "$APP_DIR" pull --ff-only
  else
    git -C "$APP_DIR" pull --ff-only
  fi
  chown -R "$APP_USER":"$APP_GROUP" "$APP_DIR"
fi

# ====== VENV ======
log "Setting up venv"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# ====== ENV ======
log "Writing .env"
SECRET_KEY=$(openssl rand -hex 32)
cat > "$APP_DIR/.env" <<EOF
SECRET_KEY=$SECRET_KEY
DATABASE_URL=sqlite:////var/www/task-platform03/app.db
UPLOAD_DIR=/var/www/task-platform03/uploads
API_AUTH_DEBUG=0

MANAGER_LOGIN=manager
MANAGER_PASSWORD=manager123
MANAGER_FIRST=Manager
MANAGER_LAST=User
MANAGER_MIDDLE=
ADMIN_LOGIN=admin
ADMIN_PASSWORD=admin123
ADMIN_FIRST=Admin
ADMIN_LAST=User
ADMIN_MIDDLE=
EOF
chown "$APP_USER":"$APP_GROUP" "$APP_DIR/.env"

# ====== RUN DIR ======
log "Preparing runtime dir"
mkdir -p /run/taskplatform
chown "$APP_USER":"$APP_GROUP" /run/taskplatform

# ====== SYSTEMD ======
log "Installing systemd service"
cp "$APP_DIR/deploy/taskplatform.service" /etc/systemd/system/taskplatform.service
systemctl daemon-reload
systemctl enable --now taskplatform

# ====== NGINX ======
log "Configuring Nginx"
cp "$APP_DIR/deploy/nginx-taskplatform.conf" /etc/nginx/sites-available/taskplatform
sed -i "s/mydomen.uz/$DOMAIN/g" /etc/nginx/sites-available/taskplatform
ln -sf /etc/nginx/sites-available/taskplatform /etc/nginx/sites-enabled/taskplatform
nginx -t
systemctl restart nginx

# ====== SSL ======
log "Requesting Let's Encrypt сертификат"
certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" --non-interactive --agree-tos -m "$EMAIL"

log "DONE!"
echo "Open: https://$DOMAIN"
echo "Android BASE_URL = https://$DOMAIN/"
echo "iOS baseURL = https://$DOMAIN"
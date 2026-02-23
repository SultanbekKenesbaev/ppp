#!/usr/bin/env bash
set -euo pipefail

# ====== CONFIG ======
DOMAIN="biychat.uz"
EMAIL="admin@biychat.uz"
APP_DIR="/var/www/task-platform03"
APP_USER="www-data"
APP_GROUP="www-data"

log() { echo -e "\n[taskplatform] $*\n"; }

# ====== PACKAGES ======
log "Installing system packages"
apt update
apt install -y python3-venv python3-pip nginx certbot python3-certbot-nginx openssl

# ====== PERMS ======
log "Fixing permissions"
mkdir -p "$APP_DIR"
chown -R "$APP_USER":"$APP_GROUP" "$APP_DIR"

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
log "Requesting Let's Encrypt certificate"
certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" --non-interactive --agree-tos -m "$EMAIL"

log "DONE!"
echo "Open: https://$DOMAIN"
echo "Android BASE_URL = https://$DOMAIN/"
echo "iOS baseURL = https://$DOMAIN"

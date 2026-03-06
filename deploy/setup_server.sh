#!/usr/bin/env bash
set -euo pipefail

# ====== CONFIG ======
DOMAIN="biychat.uz"
EMAIL="dcbaalpvlape@gmail.com"
REPO_URL="https://github.com/SultanbekKenesbaev/ppp.git"
GIT_TOKEN="${GIT_TOKEN:-}"
APP_DIR="/var/www/task-platform03"
APP_USER="www-data"
APP_GROUP="www-data"

DB_NAME="taskplatform"
DB_USER="taskplatform"
DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -hex 24)}"
DB_HOST="127.0.0.1"
DB_PORT="5432"
DATABASE_URL="postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
MIGRATE_FROM_SQLITE="${MIGRATE_FROM_SQLITE:-0}"
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
RQ_QUEUE_NAME="${RQ_QUEUE_NAME:-taskplatform}"
TASK_SEND_BATCH_SIZE="${TASK_SEND_BATCH_SIZE:-200}"

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

# ====== PACKAGES ======
log "Installing system packages"
apt update
apt install -y python3-venv python3-pip nginx git certbot python3-certbot-nginx openssl postgresql postgresql-contrib redis-server

# ====== POSTGRES ======
log "Configuring PostgreSQL"
systemctl enable --now postgresql

PG_CONF=$(sudo -u postgres psql -tAc "SHOW config_file;" | xargs)
PG_HBA=$(sudo -u postgres psql -tAc "SHOW hba_file;" | xargs)

if [[ -n "$PG_CONF" && -f "$PG_CONF" ]]; then
  sed -i -E "s/^#?listen_addresses\s*=.*/listen_addresses = 'localhost'/" "$PG_CONF"
fi
if [[ -n "$PG_HBA" && -f "$PG_HBA" ]]; then
  sed -i -E "s|^host\s+all\s+all\s+127\.0\.0\.1/32\s+.*|host    all             all             127.0.0.1/32            scram-sha-256|" "$PG_HBA"
  sed -i -E "s|^host\s+all\s+all\s+::1/128\s+.*|host    all             all             ::1/128                 scram-sha-256|" "$PG_HBA"
fi
systemctl restart postgresql
systemctl enable --now redis-server

if [[ "$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | xargs)" != "1" ]]; then
  sudo -u postgres psql -c "CREATE ROLE \"$DB_USER\" WITH LOGIN PASSWORD '$DB_PASSWORD';"
else
  sudo -u postgres psql -c "ALTER ROLE \"$DB_USER\" WITH LOGIN PASSWORD '$DB_PASSWORD';"
fi

if [[ "$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | xargs)" != "1" ]]; then
  sudo -u postgres psql -c "CREATE DATABASE \"$DB_NAME\" OWNER \"$DB_USER\";"
else
  sudo -u postgres psql -c "ALTER DATABASE \"$DB_NAME\" OWNER TO \"$DB_USER\";"
fi

# ====== APP DIR ======
log "Preparing app directory"
mkdir -p "$APP_DIR"
chown "$APP_USER":"$APP_GROUP" "$APP_DIR"

# ====== CLONE / UPDATE ======
if [[ ! -d "$APP_DIR/.git" ]]; then
  log "Cloning repository"
  git clone "$AUTH_REPO_URL" "$APP_DIR"
  chown -R "$APP_USER":"$APP_GROUP" "$APP_DIR"
else
  log "Updating repository"
  git -C "$APP_DIR" remote set-url origin "$AUTH_REPO_URL"
  git -C "$APP_DIR" pull --ff-only
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
DATABASE_URL=$DATABASE_URL
UPLOAD_DIR=/var/www/task-platform03/uploads
API_AUTH_DEBUG=0
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=20
DB_POOL_TIMEOUT=30
DB_POOL_RECYCLE=1800
REDIS_URL=$REDIS_URL
RQ_QUEUE_NAME=$RQ_QUEUE_NAME
TASK_SEND_BATCH_SIZE=$TASK_SEND_BATCH_SIZE

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

# ====== DATA MIGRATION / INIT ======
log "Preparing PostgreSQL data"
systemctl stop taskplatform || true

if [[ "$MIGRATE_FROM_SQLITE" == "1" ]]; then
  if [[ -f "$APP_DIR/app.db" ]]; then
    cp "$APP_DIR/app.db" "$APP_DIR/app.db.pre-pg.$(date +%F-%H%M%S)"
    chown "$APP_USER":"$APP_GROUP" "$APP_DIR"/app.db.pre-pg.* || true
    sudo -u "$APP_USER" env \
      SRC_SQLITE_PATH="$APP_DIR/app.db" \
      DST_DATABASE_URL="$DATABASE_URL" \
      "$APP_DIR/venv/bin/python" "$APP_DIR/scripts/migrate_sqlite_to_postgres.py" --drop-existing
  else
    echo "No SQLite file found, skipping data migration."
  fi
else
  echo "MIGRATE_FROM_SQLITE=0, skipping SQLite -> PostgreSQL migration."
fi

sudo -u "$APP_USER" bash -lc "set -a; source '$APP_DIR/.env'; set +a; cd '$APP_DIR'; '$APP_DIR/venv/bin/python' -m app.seed"

# ====== RUN DIR ======
log "Preparing runtime dir"
mkdir -p /run/taskplatform
chown "$APP_USER":"$APP_GROUP" /run/taskplatform

# ====== SYSTEMD ======
log "Installing systemd service"
cp "$APP_DIR/deploy/taskplatform.service" /etc/systemd/system/taskplatform.service
cat > /etc/systemd/system/taskplatform-rq-worker.service <<EOF
[Unit]
Description=TaskPlatform RQ Worker
After=network.target postgresql.service redis-server.service
Requires=postgresql.service redis-server.service

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$APP_DIR
Environment=FLASK_ENV=production
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/rq worker --url $REDIS_URL --name taskplatform-worker $RQ_QUEUE_NAME
Restart=always
RestartSec=3
TimeoutStartSec=90
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now taskplatform
systemctl enable --now taskplatform-rq-worker

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
echo "PostgreSQL DATABASE_URL = $DATABASE_URL"

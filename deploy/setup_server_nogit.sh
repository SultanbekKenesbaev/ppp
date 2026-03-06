#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# ====== CONFIG ======
DOMAIN="${DOMAIN:-biychat.uz}"
EMAIL="${EMAIL:-admin@biychat.uz}"
ENABLE_SSL="${ENABLE_SSL:-1}"

APP_DIR="${APP_DIR:-/var/www/task-platform03}"
APP_USER="${APP_USER:-www-data}"
APP_GROUP="${APP_GROUP:-www-data}"
APP_SERVICE="${APP_SERVICE:-taskplatform}"

SWAP_SIZE_GB="${SWAP_SIZE_GB:-2}"

DB_NAME="${DB_NAME:-taskplatform}"
DB_USER="${DB_USER:-taskplatform}"
DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -hex 24)}"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"
DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}}"
MIGRATE_FROM_SQLITE="${MIGRATE_FROM_SQLITE:-0}"

DB_POOL_SIZE="${DB_POOL_SIZE:-5}"
DB_MAX_OVERFLOW="${DB_MAX_OVERFLOW:-5}"
DB_POOL_TIMEOUT="${DB_POOL_TIMEOUT:-30}"
DB_POOL_RECYCLE="${DB_POOL_RECYCLE:-1800}"

GUNICORN_WORKERS="${GUNICORN_WORKERS:-1}"
GUNICORN_WORKER_CONNECTIONS="${GUNICORN_WORKER_CONNECTIONS:-500}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-600}"
GUNICORN_GRACEFUL_TIMEOUT="${GUNICORN_GRACEFUL_TIMEOUT:-120}"

REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
RQ_QUEUE_NAME="${RQ_QUEUE_NAME:-taskplatform}"
TASK_SEND_BATCH_SIZE="${TASK_SEND_BATCH_SIZE:-200}"

MANAGER_LOGIN="${MANAGER_LOGIN:-manager}"
MANAGER_PASSWORD="${MANAGER_PASSWORD:-manager123}"
MANAGER_FIRST="${MANAGER_FIRST:-Manager}"
MANAGER_LAST="${MANAGER_LAST:-User}"
MANAGER_MIDDLE="${MANAGER_MIDDLE:-}"

ADMIN_LOGIN="${ADMIN_LOGIN:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin123}"
ADMIN_FIRST="${ADMIN_FIRST:-Admin}"
ADMIN_LAST="${ADMIN_LAST:-User}"
ADMIN_MIDDLE="${ADMIN_MIDDLE:-}"

SECRET_KEY="${SECRET_KEY:-$(openssl rand -hex 32)}"

log() { echo -e "\n[taskplatform] $*\n"; }

as_user() {
  local user="$1"
  shift
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$user" -- "$@"
  else
    sudo -u "$user" "$@"
  fi
}

as_user_shell() {
  local user="$1"
  local cmd="$2"
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$user" -- bash -lc "$cmd"
  else
    sudo -u "$user" bash -lc "$cmd"
  fi
}

ensure_swap() {
  if [[ "$SWAP_SIZE_GB" == "0" ]]; then
    log "Swap disabled by SWAP_SIZE_GB=0"
    return
  fi

  if swapon --show | grep -q .; then
    log "Swap already configured"
    return
  fi

  log "Creating ${SWAP_SIZE_GB}G swapfile"
  if ! fallocate -l "${SWAP_SIZE_GB}G" /swapfile 2>/dev/null; then
    dd if=/dev/zero of=/swapfile bs=1M count="$((SWAP_SIZE_GB * 1024))" status=progress
  fi
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  if ! grep -q '^/swapfile ' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
}

set_pg_conf() {
  local file="$1"
  local key="$2"
  local value="$3"
  if grep -Eq "^[#[:space:]]*${key}[[:space:]]*=" "$file"; then
    sed -i -E "s|^[#[:space:]]*${key}[[:space:]]*=.*|${key} = ${value}|" "$file"
  else
    echo "${key} = ${value}" >> "$file"
  fi
}

if [[ "$EUID" -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/setup_server_nogit.sh"
  exit 1
fi

if [[ ! -f "$APP_DIR/requirements.txt" ]]; then
  echo "ERROR: $APP_DIR/requirements.txt not found. Upload project first."
  exit 1
fi

# ====== PACKAGES ======
log "Installing system packages"
apt update
apt install -y python3-venv python3-pip nginx certbot python3-certbot-nginx openssl \
  postgresql postgresql-contrib redis-server rsync ca-certificates

# ====== BASE OS TUNING ======
ensure_swap
cat > /etc/sysctl.d/99-taskplatform.conf <<EOF
vm.swappiness=10
vm.vfs_cache_pressure=50
EOF
sysctl --system >/dev/null || true

# ====== PERMISSIONS ======
log "Preparing app directory"
mkdir -p "$APP_DIR"
mkdir -p "$APP_DIR/uploads"
chown -R "$APP_USER":"$APP_GROUP" "$APP_DIR"

# ====== POSTGRES ======
log "Configuring PostgreSQL"
systemctl enable --now postgresql

PG_CONF="$(as_user postgres psql -tAc "SHOW config_file;" | xargs)"
PG_HBA="$(as_user postgres psql -tAc "SHOW hba_file;" | xargs)"

set_pg_conf "$PG_CONF" "listen_addresses" "'localhost'"
set_pg_conf "$PG_CONF" "max_connections" "100"
set_pg_conf "$PG_CONF" "shared_buffers" "'128MB'"
set_pg_conf "$PG_CONF" "effective_cache_size" "'768MB'"
set_pg_conf "$PG_CONF" "work_mem" "'4MB'"
set_pg_conf "$PG_CONF" "maintenance_work_mem" "'64MB'"
set_pg_conf "$PG_CONF" "wal_buffers" "'4MB'"

if grep -Eq '^host\s+all\s+all\s+127\.0\.0\.1/32\s+' "$PG_HBA"; then
  sed -i -E "s|^host\s+all\s+all\s+127\.0\.0\.1/32\s+.*|host    all             all             127.0.0.1/32            scram-sha-256|" "$PG_HBA"
else
  echo "host    all             all             127.0.0.1/32            scram-sha-256" >> "$PG_HBA"
fi

if grep -Eq '^host\s+all\s+all\s+::1/128\s+' "$PG_HBA"; then
  sed -i -E "s|^host\s+all\s+all\s+::1/128\s+.*|host    all             all             ::1/128                 scram-sha-256|" "$PG_HBA"
else
  echo "host    all             all             ::1/128                 scram-sha-256" >> "$PG_HBA"
fi

systemctl restart postgresql
systemctl enable --now redis-server

if [[ "$(as_user postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | xargs)" != "1" ]]; then
  as_user postgres psql -v ON_ERROR_STOP=1 -c "CREATE ROLE \"$DB_USER\" WITH LOGIN PASSWORD '$DB_PASSWORD';"
else
  as_user postgres psql -v ON_ERROR_STOP=1 -c "ALTER ROLE \"$DB_USER\" WITH LOGIN PASSWORD '$DB_PASSWORD';"
fi

if [[ "$(as_user postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | xargs)" != "1" ]]; then
  as_user postgres createdb -O "$DB_USER" "$DB_NAME"
else
  as_user postgres psql -v ON_ERROR_STOP=1 -c "ALTER DATABASE \"$DB_NAME\" OWNER TO \"$DB_USER\";"
fi

# ====== VENV ======
log "Installing Python dependencies"
as_user "$APP_USER" python3 -m venv "$APP_DIR/venv"
as_user "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip wheel
as_user "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# ====== ENV ======
log "Writing .env"
cat > "$APP_DIR/.env" <<EOF
SECRET_KEY=$SECRET_KEY
DATABASE_URL=$DATABASE_URL
UPLOAD_DIR=$APP_DIR/uploads
API_AUTH_DEBUG=0
DB_POOL_SIZE=$DB_POOL_SIZE
DB_MAX_OVERFLOW=$DB_MAX_OVERFLOW
DB_POOL_TIMEOUT=$DB_POOL_TIMEOUT
DB_POOL_RECYCLE=$DB_POOL_RECYCLE
REDIS_URL=$REDIS_URL
RQ_QUEUE_NAME=$RQ_QUEUE_NAME
TASK_SEND_BATCH_SIZE=$TASK_SEND_BATCH_SIZE

GUNICORN_WORKERS=$GUNICORN_WORKERS
GUNICORN_WORKER_CONNECTIONS=$GUNICORN_WORKER_CONNECTIONS
GUNICORN_TIMEOUT=$GUNICORN_TIMEOUT
GUNICORN_GRACEFUL_TIMEOUT=$GUNICORN_GRACEFUL_TIMEOUT

MANAGER_LOGIN=$MANAGER_LOGIN
MANAGER_PASSWORD=$MANAGER_PASSWORD
MANAGER_FIRST=$MANAGER_FIRST
MANAGER_LAST=$MANAGER_LAST
MANAGER_MIDDLE=$MANAGER_MIDDLE
ADMIN_LOGIN=$ADMIN_LOGIN
ADMIN_PASSWORD=$ADMIN_PASSWORD
ADMIN_FIRST=$ADMIN_FIRST
ADMIN_LAST=$ADMIN_LAST
ADMIN_MIDDLE=$ADMIN_MIDDLE
EOF
chown "$APP_USER":"$APP_GROUP" "$APP_DIR/.env"
chmod 640 "$APP_DIR/.env"

# ====== DATA MIGRATION / INIT ======
log "Preparing database"
systemctl stop "$APP_SERVICE" || true

if [[ "$MIGRATE_FROM_SQLITE" == "1" && -f "$APP_DIR/app.db" ]]; then
  cp "$APP_DIR/app.db" "$APP_DIR/app.db.pre-pg.$(date +%F-%H%M%S)"
  chown "$APP_USER":"$APP_GROUP" "$APP_DIR"/app.db.pre-pg.* || true
  as_user "$APP_USER" env \
    SRC_SQLITE_PATH="$APP_DIR/app.db" \
    DST_DATABASE_URL="$DATABASE_URL" \
    "$APP_DIR/venv/bin/python" "$APP_DIR/scripts/migrate_sqlite_to_postgres.py" --drop-existing
else
  log "Skipping SQLite -> PostgreSQL migration (MIGRATE_FROM_SQLITE=$MIGRATE_FROM_SQLITE)"
fi

as_user_shell "$APP_USER" "set -a; source '$APP_DIR/.env'; set +a; cd '$APP_DIR'; '$APP_DIR/venv/bin/python' -m app.seed"

# ====== SYSTEMD ======
log "Installing systemd service"
cat > "/etc/systemd/system/${APP_SERVICE}.service" <<EOF
[Unit]
Description=TaskPlatform Flask App
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$APP_DIR
Environment=FLASK_ENV=production
EnvironmentFile=$APP_DIR/.env
RuntimeDirectory=taskplatform
RuntimeDirectoryMode=0755
ExecStart=$APP_DIR/venv/bin/gunicorn -c $APP_DIR/deploy/gunicorn.conf.py "app:create_app()"
Restart=always
RestartSec=3
TimeoutStartSec=90
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

cat > "/etc/systemd/system/${APP_SERVICE}-rq-worker.service" <<EOF
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
ExecStart=$APP_DIR/venv/bin/rq worker --url $REDIS_URL --name ${APP_SERVICE}-worker $RQ_QUEUE_NAME
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
systemctl enable --now "$APP_SERVICE"
systemctl enable --now "${APP_SERVICE}-rq-worker"

# ====== NGINX ======
log "Configuring Nginx"
cp "$APP_DIR/deploy/nginx-taskplatform.conf" /etc/nginx/sites-available/taskplatform
sed -i "s/mydomen.uz/$DOMAIN/g" /etc/nginx/sites-available/taskplatform
ln -sf /etc/nginx/sites-available/taskplatform /etc/nginx/sites-enabled/taskplatform
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl restart nginx

# ====== SSL ======
if [[ "$ENABLE_SSL" == "1" ]]; then
  log "Requesting Let's Encrypt certificate"
  certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" || {
    log "WARNING: certbot failed. Check DNS and run certbot later."
  }
else
  log "SSL skipped (ENABLE_SSL=$ENABLE_SSL)"
fi

systemctl restart "$APP_SERVICE"
systemctl status "$APP_SERVICE" --no-pager -n 40 || true
systemctl status "${APP_SERVICE}-rq-worker" --no-pager -n 20 || true

log "DONE"
echo "Site: https://$DOMAIN"
echo "DATABASE_URL: $DATABASE_URL"
echo "DB_PASSWORD: $DB_PASSWORD"

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SERVER="${SERVER:-${1:-}}"
if [[ -z "$SERVER" ]]; then
  echo "Usage: SERVER=root@your_vps_ip bash deploy/deploy_from_laptop.sh"
  exit 1
fi

SSH_PORT="${SSH_PORT:-22}"
APP_DIR="${APP_DIR:-/var/www/task-platform03}"

DOMAIN="${DOMAIN:-biychat.uz}"
EMAIL="${EMAIL:-admin@biychat.uz}"
ENABLE_SSL="${ENABLE_SSL:-1}"

DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -hex 24)}"
MIGRATE_FROM_SQLITE="${MIGRATE_FROM_SQLITE:-0}"
COPY_SQLITE="${COPY_SQLITE:-0}"
COPY_UPLOADS="${COPY_UPLOADS:-0}"

SWAP_SIZE_GB="${SWAP_SIZE_GB:-2}"
DB_POOL_SIZE="${DB_POOL_SIZE:-5}"
DB_MAX_OVERFLOW="${DB_MAX_OVERFLOW:-5}"
GUNICORN_WORKERS="${GUNICORN_WORKERS:-1}"
GUNICORN_WORKER_CONNECTIONS="${GUNICORN_WORKER_CONNECTIONS:-500}"

if [[ "$COPY_SQLITE" == "1" ]]; then
  MIGRATE_FROM_SQLITE="1"
fi

if ! command -v rsync >/dev/null 2>&1; then
  echo "ERROR: rsync is not installed on your laptop. Install it first."
  exit 1
fi

SSH_OPTS=(-p "$SSH_PORT" -o StrictHostKeyChecking=accept-new)
RSYNC_SSH="ssh -p $SSH_PORT -o StrictHostKeyChecking=accept-new"

echo "[deploy] Uploading project to $SERVER:$APP_DIR"
ssh "${SSH_OPTS[@]}" "$SERVER" "mkdir -p '$APP_DIR'"

# rsync is required on remote side for file sync.
ssh "${SSH_OPTS[@]}" "$SERVER" "command -v rsync >/dev/null 2>&1 || (export DEBIAN_FRONTEND=noninteractive; apt-get update -y && apt-get install -y rsync)"

RSYNC_EXCLUDES=(
  --exclude '.git/'
  --exclude '.idea/'
  --exclude '.vscode/'
  --exclude 'venv/'
  --exclude '__pycache__/'
  --exclude '*.pyc'
  --exclude '.DS_Store'
  --exclude '.env'
  --exclude '.env.*'
  --exclude 'uploads/'
  --exclude 'app.db'
)

rsync -az --delete -e "$RSYNC_SSH" "${RSYNC_EXCLUDES[@]}" "$PROJECT_DIR/" "$SERVER:$APP_DIR/"

if [[ "$COPY_SQLITE" == "1" ]]; then
  if [[ -f "$PROJECT_DIR/app.db" ]]; then
    echo "[deploy] Copying app.db"
    rsync -az -e "$RSYNC_SSH" "$PROJECT_DIR/app.db" "$SERVER:$APP_DIR/app.db"
  else
    echo "[deploy] WARNING: COPY_SQLITE=1 but app.db not found locally"
  fi
fi

if [[ "$COPY_UPLOADS" == "1" && -d "$PROJECT_DIR/uploads" ]]; then
  echo "[deploy] Copying uploads/"
  rsync -az --delete -e "$RSYNC_SSH" "$PROJECT_DIR/uploads/" "$SERVER:$APP_DIR/uploads/"
fi

printf -v REMOTE_CMD \
  "DOMAIN=%q EMAIL=%q ENABLE_SSL=%q APP_DIR=%q DB_PASSWORD=%q MIGRATE_FROM_SQLITE=%q SWAP_SIZE_GB=%q DB_POOL_SIZE=%q DB_MAX_OVERFLOW=%q GUNICORN_WORKERS=%q GUNICORN_WORKER_CONNECTIONS=%q bash %q" \
  "$DOMAIN" "$EMAIL" "$ENABLE_SSL" "$APP_DIR" "$DB_PASSWORD" "$MIGRATE_FROM_SQLITE" "$SWAP_SIZE_GB" "$DB_POOL_SIZE" "$DB_MAX_OVERFLOW" "$GUNICORN_WORKERS" "$GUNICORN_WORKER_CONNECTIONS" "$APP_DIR/deploy/setup_server_nogit.sh"

echo "[deploy] Running server bootstrap script"
ssh "${SSH_OPTS[@]}" "$SERVER" "$REMOTE_CMD"

echo "[deploy] DONE"
echo "[deploy] URL: https://$DOMAIN"
echo "[deploy] DB_PASSWORD: $DB_PASSWORD"

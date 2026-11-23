#!/usr/bin/env bash
# Runs on the VPS (called by GitHub Actions after it has done `git reset --hard origin/main`).
# Safe to run by hand too:  bash deploy/deploy.sh
set -euo pipefail

APP_DIR=/home/ubuntu/apps/mednevo/symptom_checker/backend
VENV="$APP_DIR/venv"
SERVICE=datapprove
SOCK=/run/datapprove/gunicorn.sock

cd "$APP_DIR"

echo "==> Deploying $(git rev-parse --short HEAD): $(git log -1 --pretty=%s)"

# shared/.env is the single source of config; load it for manage.py commands
set -a; . "$APP_DIR/shared/.env"; set +a

if [ ! -d "$KNOWLEDGE_DB_DIR/data" ]; then
  echo "!! $KNOWLEDGE_DB_DIR/data missing - seed it once:  cp -a $APP_DIR/database $KNOWLEDGE_DB_DIR" >&2
  exit 1
fi

echo "==> Installing dependencies"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r requirements.txt

echo "==> Django checks"
"$VENV/bin/python" manage.py check --deploy

echo "==> Migrating"
"$VENV/bin/python" manage.py migrate --noinput

echo "==> Collecting static"
"$VENV/bin/python" manage.py collectstatic --noinput --clear -v 0

echo "==> Restarting $SERVICE"
sudo systemctl restart "$SERVICE"

echo "==> Health check"
for i in $(seq 1 15); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --unix-socket "$SOCK" \
         -H 'Host: data.api.mednevo.com' -H 'X-Forwarded-Proto: https' \
         http://localhost/api/modules/ || true)
  if [ "$code" = "200" ]; then
    echo "==> OK (HTTP 200) - deploy finished"
    exit 0
  fi
  sleep 2
done

echo "!! Health check failed (last HTTP code: ${code:-none})" >&2
sudo systemctl status "$SERVICE" --no-pager -l | tail -n 30 >&2 || true
exit 1

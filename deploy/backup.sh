#!/usr/bin/env bash
# Daily backup of Postgres + doctor-edited data files. Keeps 14 days.
# crontab -e (as ubuntu):
#   30 2 * * * /home/ubuntu/apps/mednevo/symptom_checker/backend/deploy/backup.sh >> /home/ubuntu/apps/mednevo/symptom_checker/backend/backups/backup.log 2>&1
set -euo pipefail

APP_DIR=/home/ubuntu/apps/mednevo/symptom_checker/backend
DEST=/home/ubuntu/apps/mednevo/symptom_checker/backend/backups
STAMP=$(date +%Y%m%d-%H%M%S)

set -a; . "$APP_DIR/shared/.env"; set +a
mkdir -p "$DEST"

if [ -n "${DATABASE_URL:-}" ]; then
  pg_dump "$DATABASE_URL"
else
  PGPASSWORD="$DB_PASS" pg_dump -h "${POSTGRES_HOST:-127.0.0.1}" -p "${DB_PORT:-5432}" -U "$DB_USER" "$DB_NAME"
fi | gzip > "$DEST/db-$STAMP.sql.gz"
tar -czf "$DEST/data-$STAMP.tar.gz" -C "$(dirname "$KNOWLEDGE_DB_DIR")" "$(basename "$KNOWLEDGE_DB_DIR")"

find "$DEST" -name 'db-*.sql.gz'   -mtime +14 -delete
find "$DEST" -name 'data-*.tar.gz' -mtime +14 -delete
echo "$(date -Is) backup ok: $STAMP"

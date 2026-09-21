#!/usr/bin/env bash
# Nightly Postgres dump. Add to cron:
#   15 2 * * * /opt/nightjar/deploy/backup.sh >> /var/log/nightjar-backup.log 2>&1
#
# A backup you have never restored is a hypothesis, not a backup. Test it.
set -euo pipefail

cd "$(dirname "$0")/.."
source .env

DEST="${BACKUP_DIR:-/var/backups/nightjar}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
mkdir -p "$DEST"

STAMP=$(date +%Y%m%d-%H%M%S)
FILE="$DEST/nightjar-$STAMP.sql.gz"

docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$FILE"
echo "$(date -Is) wrote $FILE ($(du -h "$FILE" | cut -f1))"

find "$DEST" -name 'nightjar-*.sql.gz' -mtime "+$KEEP_DAYS" -delete

# Offsite. Same-machine backups do not survive the failure they exist for.
if [ -n "${RCLONE_REMOTE:-}" ]; then
  rclone copy "$FILE" "$RCLONE_REMOTE" && echo "$(date -Is) offsite ok"
fi

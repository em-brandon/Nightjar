#!/usr/bin/env bash
# Nightly Postgres dump, encrypted with age before it is written. Add to cron:
#   15 2 * * * /opt/nightjar/deploy/backup.sh >> /var/log/nightjar-backup.log 2>&1
#
# The dump holds every site's ingest secret and all the error text, so it is encrypted to a PUBLIC key
# (BACKUP_AGE_RECIPIENT in .env). The server can write backups but not read them; the private key lives
# somewhere else (docs/DATA-PROTECTION.md, DEPLOY.md section 6). Restore with deploy/restore.sh.
#
# A backup you have never restored is a hypothesis, not a backup. Test it.
set -euo pipefail

cd "$(dirname "$0")/.."
source .env

DEST="${BACKUP_DIR:-/var/backups/nightjar}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
mkdir -p "$DEST"

# Refuse to write a readable copy by accident: a missing key would otherwise look like a working backup.
if [ -z "${BACKUP_AGE_RECIPIENT:-}" ]; then
  echo "$(date -Is) BACKUP_AGE_RECIPIENT is not set, so nothing was backed up. See DEPLOY.md section 6." >&2
  exit 1
fi

STAMP=$(date +%Y%m%d-%H%M%S)
FILE="$DEST/nightjar-$STAMP.sql.gz.age"

# Write to a temp name and rename, so a failed run never leaves a truncated file that looks like a backup.
# pipefail makes a failed pg_dump or age fail the whole pipeline.
docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip | age -r "$BACKUP_AGE_RECIPIENT" > "$FILE.part"
mv "$FILE.part" "$FILE"
echo "$(date -Is) wrote $FILE ($(du -h "$FILE" | cut -f1))"

find "$DEST" \( -name 'nightjar-*.sql.gz.age' -o -name 'nightjar-*.sql.gz.age.part' \) -mtime "+$KEEP_DAYS" -delete
# Plain backups from before encryption was added: remove them on the same schedule rather than leave them readable.
find "$DEST" -name 'nightjar-*.sql.gz' -mtime "+$KEEP_DAYS" -delete

# Offsite. Same-machine backups do not survive the failure they exist for.
if [ -n "${RCLONE_REMOTE:-}" ]; then
  rclone copy "$FILE" "$RCLONE_REMOTE" && echo "$(date -Is) offsite ok"
fi

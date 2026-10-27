#!/usr/bin/env bash
# Restore an encrypted backup into a database. Usage:
#   deploy/restore.sh <backup.sql.gz.age> <identity-file> [database-url]
#
# The identity file is the age private key, kept off this server. With no database URL the dump is only
# decrypted and counted, which proves the file and the key work without touching anything.
# Restore into a CLEAN database: the dump creates tables and does not drop existing ones.
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: $0 <backup.sql.gz.age> <identity-file> [database-url]" >&2
  exit 2
fi
FILE=$1; IDENTITY=$2; TARGET=${3:-}

if [ -z "$TARGET" ]; then
  # Only claim success if the whole pipeline did, and the dump is not empty.
  LINES=$(age -d -i "$IDENTITY" "$FILE" | gunzip | wc -l)
  [ "$LINES" -gt 0 ] || { echo "decrypted nothing: wrong key or damaged file" >&2; exit 1; }
  echo "decrypted ok, lines of SQL: $LINES"
else
  age -d -i "$IDENTITY" "$FILE" | gunzip | psql -v ON_ERROR_STOP=1 "$TARGET"
fi

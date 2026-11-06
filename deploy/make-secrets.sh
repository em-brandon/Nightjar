#!/usr/bin/env bash
# Create the secret files the production stack mounts, in ./secrets (or the directory you name).
#
# Production keeps passwords and tokens in files, not in environment variables, because `docker inspect`, process
# listings and crash dumps show environment variables. docker-compose.yml mounts each file under /run/secrets, and the
# hub reads each by the name of the setting it fills (hub/config.py).
#
#   ./deploy/make-secrets.sh            create what is missing, never overwrite what is there
#   ./deploy/make-secrets.sh /path      the same, in another directory
#   ./deploy/make-secrets.sh --check    change nothing; fail if any secret is readable by more than it should be
#
# Run it once on the VPS, as root (sudo), before `docker compose up`. It needs openssl, and a .env with POSTGRES_USER and
# POSTGRES_DB. As root it locks every file to the one account that needs it (see lock below); as anyone else it cannot,
# says so, and leaves the files world-readable inside the private directory.
set -euo pipefail

cd "$(dirname "$0")/.."
CHECK=0
if [ "${1:-}" = "--check" ]; then
  CHECK=1
  shift
fi
DIR="${1:-secrets}"
HUB_UID=10001 # the unprivileged user the hub runs as (Dockerfile)

# Files only the database container reads. Everything else is the hub's.
DB_ONLY="postgres_password"

if [ "$CHECK" = 1 ]; then
  bad=0
  [ -d "$DIR" ] || { echo "no secrets directory at $DIR" >&2; exit 1; }
  mode=$(stat -c '%a' "$DIR")
  [ "$mode" = "700" ] || { echo "BAD  $DIR is mode $mode, want 700" >&2; bad=1; }
  for f in "$DIR"/*; do
    [ -e "$f" ] || continue
    mode=$(stat -c '%a' "$f")
    case "$mode" in
      *00) ;; # nothing for group or others
      *) echo "BAD  $f is mode $mode: readable by group or others" >&2; bad=1 ;;
    esac
  done
  [ "$bad" = 0 ] && echo "secrets: every file is private to its owner"
  exit "$bad"
fi

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
: "${POSTGRES_USER:?set POSTGRES_USER in .env first}"
: "${POSTGRES_DB:?set POSTGRES_DB in .env first}"
command -v openssl >/dev/null || { echo "openssl is needed to generate secrets" >&2; exit 1; }

# Docker bind-mounts each file with the owner and mode it has on the host, and the hub runs as an unprivileged user. So:
#   * as root, each file goes to the one account that reads it, mode 400: the hub's files to the hub's uid, the database
#     password to root (the Postgres entrypoint reads it as root before dropping privileges). Nothing is world-readable,
#     so a loosened directory or a careless copy no longer exposes anything.
#   * as anyone else we cannot change the owner, so the file stays readable by others (644) and the 700 directory is the
#     only thing keeping other accounts out. We say so, because that is the weaker setup.
umask 077
mkdir -p "$DIR"
chmod 700 "$DIR"
if [ "$(id -u)" = 0 ]; then ROOT=1; else ROOT=0; fi
weak=0

lock() { # lock <name>: give the file its owner and mode (also repairs a file that was loosened since)
  local file="$DIR/$1"
  if [ "$ROOT" = 1 ]; then
    case " $DB_ONLY " in
      *" $1 "*) chown 0:0 "$file" ;;
      *) chown "$HUB_UID:$HUB_UID" "$file" ;;
    esac
    chmod 400 "$file"
  else
    chmod 644 "$file" 2>/dev/null || true
    weak=1
  fi
}

made=()
kept=()

# A value must be exactly itself: no carriage return and no newline, whatever platform openssl ran on. (openssl on Windows
# ends its output with CRLF, and a stray \r in a token or a database URL is the kind of fault that costs an evening.)
clean() { tr -d '\r\n'; }

put() { # put <name> <value>: write only if the file does not exist yet
  local file="$DIR/$1"
  if [ -e "$file" ]; then
    kept+=("$1")
    lock "$1"
    return
  fi
  printf '%s' "$2" | clean >"$file"
  lock "$1"
  made+=("$1")
}

# Generated. A hex password is safe to put inside a database URL, where base64's / + = would not be.
put postgres_password "$(openssl rand -hex 32 | clean)"
# Every site's ingest secret is derived from this (hub/derive.py). It is the one file to keep a copy of apart from the
# database: lose it and every app needs a new secret.
put secret_master_key "$(openssl rand -base64 32 | clean)"
put pushover_callback_token "$(openssl rand -hex 24 | clean)"

# Built from the password above, so the hub never needs it in its environment.
put database_url "postgresql+asyncpg://${POSTGRES_USER}:$(clean <"$DIR/postgres_password")@db:5432/${POSTGRES_DB}"

# Yours to fill in. Created empty so Compose can mount them: an empty file means "not set", which is how the hub treats
# Pushover until you add the real values (the Alerts tab says so plainly while they are empty).
for name in pushover_token pushover_user deadman_url presence_token; do
  put "$name" ""
done

if [ "$ROOT" = 1 ]; then
  echo "secrets directory: $DIR (mode 700, each file mode 400 and owned by the one account that reads it)"
else
  echo "secrets directory: $DIR (mode 700)"
fi
if [ "$weak" = 1 ]; then
  echo "WARNING: not run as root, so the files are readable by every account on this machine (the directory is the only guard)." >&2
  echo "         Run it again with sudo to lock each one to its owner. ./deploy/make-secrets.sh --check reports what is loose." >&2
fi
[ ${#made[@]} -gt 0 ] && echo "created: ${made[*]}"
[ ${#kept[@]} -gt 0 ] && echo "left alone (already there): ${kept[*]}"
echo
echo "Still to do by hand:"
echo "  - put your Pushover application token in $DIR/pushover_token and your user key in $DIR/pushover_user"
echo "  - put your healthchecks.io ping URL in $DIR/deadman_url"
echo "  - create your console account: docker compose exec hub python -m hub.operator_cli create <username> (there is no shared console token any more)"
echo "  - copy secret_master_key into your password manager too. Every site's ingest secret is worked out from it, and it is not in the database"
echo "  - back this directory up somewhere other than this server. It is not in git, and losing it loses the keys."

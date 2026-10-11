#!/usr/bin/env bash
# Create the secret files the production stack mounts, in ./secrets (or the directory you name).
#
# Production keeps passwords and tokens in files, not in environment variables, because `docker inspect`, process
# listings and crash dumps show environment variables. docker-compose.yml mounts each file under /run/secrets, and the
# hub reads each by the name of the setting it fills (hub/config.py).
#
#   ./deploy/make-secrets.sh            create what is missing, never overwrite what is there
#   ./deploy/make-secrets.sh /path      the same, in another directory
#
# Run it once on the VPS, before `docker compose up`. It needs openssl, and a .env with POSTGRES_USER and POSTGRES_DB.
set -euo pipefail

cd "$(dirname "$0")/.."
DIR="${1:-secrets}"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
: "${POSTGRES_USER:?set POSTGRES_USER in .env first}"
: "${POSTGRES_DB:?set POSTGRES_DB in .env first}"
command -v openssl >/dev/null || { echo "openssl is needed to generate secrets" >&2; exit 1; }

# Only the owner can list or enter this directory. Each file is world-readable inside it because Docker bind-mounts it
# as it is and the hub runs as an unprivileged user, so the directory is what keeps other accounts out.
umask 077
mkdir -p "$DIR"
chmod 700 "$DIR"

made=()
kept=()

# A value must be exactly itself: no carriage return and no newline, whatever platform openssl ran on. (openssl on Windows
# ends its output with CRLF, and a stray \r in a token or a database URL is the kind of fault that costs an evening.)
clean() { tr -d '\r\n'; }

put() { # put <name> <value>: write only if the file does not exist yet
  local file="$DIR/$1"
  if [ -e "$file" ]; then
    kept+=("$1")
    return
  fi
  printf '%s' "$2" | clean >"$file"
  chmod 644 "$file"
  made+=("$1")
}

# Generated. A hex password is safe to put inside a database URL, where base64's / + = would not be.
put postgres_password "$(openssl rand -hex 32 | clean)"
put console_token "$(openssl rand -base64 32 | clean)"
put pushover_callback_token "$(openssl rand -hex 24 | clean)"

# Built from the password above, so the hub never needs it in its environment.
put database_url "postgresql+asyncpg://${POSTGRES_USER}:$(clean <"$DIR/postgres_password")@db:5432/${POSTGRES_DB}"

# Yours to fill in. Created empty so Compose can mount them: an empty file means "not set", which is how the hub treats
# Pushover until you add the real values (the Alerts tab says so plainly while they are empty).
for name in pushover_token pushover_user deadman_url presence_token; do
  put "$name" ""
done

echo "secrets directory: $DIR (mode 700)"
[ ${#made[@]} -gt 0 ] && echo "created: ${made[*]}"
[ ${#kept[@]} -gt 0 ] && echo "left alone (already there): ${kept[*]}"
echo
echo "Still to do by hand:"
echo "  - put your Pushover application token in $DIR/pushover_token and your user key in $DIR/pushover_user"
echo "  - put your healthchecks.io ping URL in $DIR/deadman_url"
echo "  - copy the console token out of $DIR/console_token into your password manager: it is what you sign in with"
echo "  - back this directory up somewhere other than this server. It is not in git, and losing it loses the keys."

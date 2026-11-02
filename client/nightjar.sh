#!/bin/sh
# nightjar.sh: report to a Nightjar hub from cron or CI. POSIX sh, needs only curl and openssl, so a host with no
# Node or Python can use it and anyone can read every line before trusting it with a site's secret.
#
#   nightjar.sh heartbeat <name> [--every 5m|2h|1d|SECONDS] [--grace 90s] [--kind job|app] [--fail "why"]
#   nightjar.sh deploy    <ref>  <running|success|failed> [--progress 0-100]
#
# Configuration, from the environment (the same three values every reporter takes):
#   NIGHTJAR_URL     the hub, e.g. https://nightjar.example.co.ke  (must be https:// unless it is localhost)
#   NIGHTJAR_KEY     the site's ingest key
#   NIGHTJAR_SECRET  the site's ingest secret. Signs the request; it is never sent. Empty = unsigned (dev hubs only)
#
# Like every reporter it must never break the thing it reports on. A hub that is down, a bad URL or a missing tool
# prints one line to stderr and exits 0, so `backup.sh && nightjar.sh heartbeat backup` never turns a Nightjar outage
# into a failed backup. NIGHTJAR_STRICT=1 makes failures exit 1, for trying it out by hand.
#
# Cron: a heartbeat belongs at the END of a successful run, and says how often to expect it:
#   0 3 * * *  /opt/backup.sh && /opt/nightjar.sh heartbeat nightly-backup --every 1d
# A run that failed can say so, which opens its own incident even though the job is alive:
#   0 3 * * *  /opt/backup.sh || /opt/nightjar.sh heartbeat nightly-backup --every 1d --fail "backup.sh exited $?"

warn() { printf 'nightjar: %s\n' "$*" >&2; }
bail() { warn "$*"; if [ "${NIGHTJAR_STRICT:-}" = "1" ]; then exit 1; fi; exit 0; }
usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' >&2; if [ "${NIGHTJAR_STRICT:-}" = "1" ]; then exit 2; fi; exit 0; }

# 30s, 5m, 2h, 1d or a bare number of seconds -> seconds. Prints nothing if it is not one of those.
seconds() {
  case "$1" in
    *[!0-9smhd]* | "" ) return 1 ;;
  esac
  n="${1%[smhd]}"
  case "$n" in *[!0-9]* | "" ) return 1 ;; esac
  case "$1" in
    *s) echo "$n" ;;
    *m) echo $((n * 60)) ;;
    *h) echo $((n * 3600)) ;;
    *d) echo $((n * 86400)) ;;
    *)  echo "$n" ;;
  esac
}

# A string as a JSON string body: backslash, quote, and control characters escaped. Nothing else is touched.
json_str() {
  printf '%s' "$1" | awk 'BEGIN { ORS = "" }
    { gsub(/\\/, "\\\\"); gsub(/"/, "\\\""); gsub(/\t/, "\\t"); gsub(/\r/, "\\r"); if (NR > 1) printf "\\n"; print }'
}

command -v curl >/dev/null 2>&1 || bail "curl is not installed"
[ -n "${NIGHTJAR_URL:-}" ] || bail "NIGHTJAR_URL is not set"
[ -n "${NIGHTJAR_KEY:-}" ] || bail "NIGHTJAR_KEY is not set"
base="${NIGHTJAR_URL%/}"
# The same rule the other reporters follow: a secret-signed report over plain http can be read and replayed on the wire.
case "$base" in
  https://*) ;;
  http://localhost* | http://127.0.0.1* | http://\[::1\]*) ;;
  *) bail "NIGHTJAR_URL must be https:// (plain http is only allowed for localhost)" ;;
esac

# POST a JSON body to a path, signed when there is a secret.
post() {
  path="$1"; body="$2"
  set -- -sS -o /dev/null -w '%{http_code}' --max-time 10 -X POST "$base$path" -H 'Content-Type: application/json'
  if [ -n "${NIGHTJAR_SECRET:-}" ]; then
    command -v openssl >/dev/null 2>&1 || bail "openssl is needed to sign (or unset NIGHTJAR_SECRET for a dev hub)"
    ts="$(date +%s)"
    # The timestamp is inside the signature, so a captured request cannot be replayed later.
    mac="$(printf '%s.%s' "$ts" "$body" | openssl dgst -sha256 -hmac "$NIGHTJAR_SECRET" | sed 's/^.* //')"
    set -- "$@" -H "X-Hub-Timestamp: $ts" -H "X-Hub-Signature: sha256=$mac"
  fi
  code="$(curl "$@" -d "$body" 2>/dev/null)" || bail "could not reach the hub"
  case "$code" in
    2??) ;;
    401) bail "the hub refused the signature (HTTP 401): check NIGHTJAR_SECRET, and that the clock is right" ;;
    404) bail "the hub does not know this key (HTTP 404): check NIGHTJAR_KEY" ;;
    422) bail "the hub rejected the request (HTTP 422): check the arguments" ;;
    *)   bail "the hub answered HTTP $code" ;;
  esac
}

cmd="${1:-}"
[ -n "$cmd" ] || usage
shift

case "$cmd" in
  heartbeat)
    name="${1:-}"
    [ -n "$name" ] || bail "heartbeat needs a name, e.g. nightly-backup"
    shift
    fields=""; add() { fields="${fields:+$fields,}$1"; }
    while [ $# -gt 0 ]; do
      [ $# -ge 2 ] || bail "$1 needs a value"
      case "$1" in
        --every) s="$(seconds "${2:-}")" || bail "--every wants 30s, 5m, 2h, 1d or seconds"; add "\"interval_s\":$s"; shift 2 ;;
        --grace) s="$(seconds "${2:-}")" || bail "--grace wants 30s, 5m, 2h, 1d or seconds"; add "\"grace_s\":$s"; shift 2 ;;
        --kind)
          case "${2:-}" in job | app) add "\"kind\":\"$2\"" ;; *) bail "--kind is job or app" ;; esac
          shift 2 ;;
        --fail) add '"status":"fail"'; if [ -n "${2:-}" ]; then add "\"message\":\"$(json_str "$2")\""; fi; shift 2 ;;
        *) bail "unknown option for heartbeat: $1" ;;
      esac
    done
    post "/ingest/$NIGHTJAR_KEY/heartbeat/$name" "{$fields}"
    ;;
  deploy)
    ref="${1:-}"; status="${2:-}"
    [ -n "$ref" ] || bail "deploy needs a ref, e.g. main@1f9c4a2"
    case "$status" in running | success | failed) ;; *) bail "deploy status is running, success or failed" ;; esac
    shift 2
    fields="\"ref\":\"$(json_str "$ref")\",\"status\":\"$status\""
    while [ $# -gt 0 ]; do
      [ $# -ge 2 ] || bail "$1 needs a value"
      case "$1" in
        --progress)
          case "${2:-}" in *[!0-9]* | "") bail "--progress wants 0 to 100" ;; esac
          [ "$2" -le 100 ] || bail "--progress wants 0 to 100"
          fields="$fields,\"progress\":$2"; shift 2 ;;
        *) bail "unknown option for deploy: $1" ;;
      esac
    done
    post "/ingest/$NIGHTJAR_KEY/deploy" "{$fields}"
    ;;
  -h | --help | help) usage ;;
  *) bail "unknown command: $cmd (try heartbeat or deploy)" ;;
esac

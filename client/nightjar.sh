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

# HMAC-SHA256 of standard input, as lowercase hex, keyed by $1.
#
# `openssl dgst -hmac "$key"` would put the secret on a command line, where every user on the machine can read it with `ps`
# (or /proc/<pid>/cmdline) for as long as the command runs. openssl has no way to take an HMAC key from a file or a pipe, so
# this builds HMAC from plain hashes instead (RFC 2104: H((K xor opad) || H((K xor ipad) || message))). The key only ever
# travels through shell variables, printf (a builtin in every shell that matters, so it is never an argv) and pipes.
hmac_hex() {
  key="$1"
  if [ "$(printf '%s' "$key" | wc -c)" -gt 64 ]; then
    keyhex="$(printf '%s' "$key" | openssl dgst -sha256 -binary | od -An -v -tx1 | tr -d ' \n')"  # a long key is hashed first
  else
    keyhex="$(printf '%s' "$key" | od -An -v -tx1 | tr -d ' \n')"
  fi
  # Plain shell arithmetic and parameter expansion, no process per byte: a signature should not cost hundreds of forks.
  ipad=""; opad=""; i=0; rest="$keyhex"
  while [ "$i" -lt 64 ]; do
    b="${rest%"${rest#??}"}"  # the next two hex digits, or nothing once the key is used up
    rest="${rest#??}"
    [ -n "$b" ] || b=00  # a short key is padded with zero bytes
    v=$((0x$b ^ 0x36)); ipad="$ipad\\$((v / 64))$((v / 8 % 8))$((v % 8))"
    v=$((0x$b ^ 0x5c)); opad="$opad\\$((v / 64))$((v / 8 % 8))$((v % 8))"
    i=$((i + 1))
  done
  # straight down the pipe, never into a variable: the inner hash is binary and may contain a NUL byte
  { printf "$ipad"; cat; } | openssl dgst -sha256 -binary | { printf "$opad"; cat; } | openssl dgst -sha256 -hex | sed 's/^.* //'
  unset key keyhex ipad opad b i v rest
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
#
# The signature headers go to curl on standard input (`-K -`), not on its command line: a signature is good for five minutes,
# and a command line is readable by every user on the machine. The body stays on the command line; it holds a job name or a
# short message, not a secret.
post() {
  path="$1"; body="$2"; config=""
  set -- -sS -o /dev/null -w '%{http_code}' --max-time 10 -X POST "$base$path" -H 'Content-Type: application/json'
  if [ -n "${NIGHTJAR_SECRET:-}" ]; then
    command -v openssl >/dev/null 2>&1 || bail "openssl is needed to sign (or unset NIGHTJAR_SECRET for a dev hub)"
    for tool in od wc tr sed; do command -v "$tool" >/dev/null 2>&1 || bail "$tool is needed to sign"; done
    ts="$(date +%s)"
    # The timestamp is inside the signature, so a captured request cannot be replayed later.
    mac="$(printf '%s.%s' "$ts" "$body" | hmac_hex "$NIGHTJAR_SECRET")"
    config="$(printf 'header = "X-Hub-Timestamp: %s"\nheader = "X-Hub-Signature: sha256=%s"' "$ts" "$mac")"
  fi
  code="$(printf '%s\n' "$config" | curl "$@" -K - -d "$body" 2>/dev/null)" || bail "could not reach the hub"
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

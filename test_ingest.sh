#!/usr/bin/env bash
# Prove the fingerprint works: send the SAME crash many times with different
# details, and check that Nightjar files it as ONE issue.
#
# Usage:  ./test_ingest.sh <ingest_key> [count]        (count defaults to 200)
#
# Against a production hub (signatures on), give it the site's secret and URL:
#   NIGHTJAR_URL=https://nightjar.example.co.ke NIGHTJAR_SECRET=<ingest_secret> \
#     ./test_ingest.sh <ingest_key>
set -euo pipefail

KEY="${1:?pass the ingest key from seed.py}"
COUNT="${2:-200}"
BASE="${NIGHTJAR_URL:-http://localhost:8000}"
SECRET="${NIGHTJAR_SECRET:-}"
URL="${BASE%/}/ingest/${KEY}/event"

ids="$(mktemp)"
trap 'rm -f "$ids"' EXIT

cat <<EOF
Sending $COUNT copies of ONE crash to $BASE

In a real outage the same error repeats, but a few details change every time
(an address, a retry count, an ID). These are $COUNT occurrences of one bug:

$(printf '  %-6s %s\n' '#1' 'connection to server at "10.0.0.1" failed after 1 retries')
$(printf '  %-6s %s\n' '#2' 'connection to server at "10.0.0.2" failed after 2 retries')
  ...
$(printf '  %-6s %s\n' "#$COUNT" "connection to server at \"10.0.0.$COUNT\" failed after $COUNT retries")

To a person that is ONE bug. Nightjar should see it the same way: it blanks out the
parts that change (quoted text and numbers) and hashes what is left, so all of these
should land in a single issue.

EOF

last=""
for i in $(seq 1 "$COUNT"); do
  body="{\"exception_type\":\"OperationalError\",\"message\":\"connection to server at \\\"10.0.0.${i}\\\" failed after ${i} retries\",\"frames\":[{\"module\":\"db/session.py\",\"function\":\"connect\",\"lineno\":41},{\"module\":\"routes/orders.py\",\"function\":\"list_orders\",\"lineno\":88}],\"level\":\"error\"}"

  # With signatures on, the hub wants an HMAC of the exact body, like a real reporter sends.
  sig=()
  if [ -n "$SECRET" ]; then
    mac="$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')"
    sig=(-H "X-Hub-Signature: sha256=${mac}")
  fi

  last="$(curl -s -X POST "$URL" -H 'Content-Type: application/json' "${sig[@]}" -d "$body" || true)"
  # the issue this occurrence was filed under, or "none" if the request failed
  echo "$(printf '%s' "$last" | grep -o '"issue_id":[0-9]*' | head -1 | cut -d: -f2 || true)" >> "$ids"

  if [ $((i % 50)) -eq 0 ]; then printf '  sent %s of %s\n' "$i" "$COUNT"; fi
done

# how many requests came back without an issue id, and how many different issues there were
# `|| true` on each: with pipefail, a grep that finds nothing (every request failed) would
# otherwise abort the script before it could explain what went wrong.
failed="$(grep -c '^$' "$ids" || true)"
distinct="$(grep -v '^$' "$ids" | sort -u | wc -l | tr -d ' ' || true)"
issue_id="$(grep -v '^$' "$ids" | sort -u | head -1 || true)"

field() { printf '%s' "$last" | grep -o "\"$1\":[^,}]*" | head -1 | cut -d: -f2- | tr -d '"'; }

echo
echo "Result"
echo "  requests that failed              : $failed"
if [ "$distinct" -gt 0 ]; then
  echo "  different issues they landed in   : $distinct${issue_id:+   (first: issue $issue_id)}"
  echo "  fingerprint                       : $(field fingerprint)"
  echo "  severity                          : $(field severity)   (derived from the crash's level, never chosen by hand)"
  echo "  times this issue has been seen    : $(field event_count)   (this grows by $COUNT each time you run the test)"
  echo "  state_change on the last one      : $(field state_change)"
  echo "      new / regression / spike would alert; null means \"already known, stay quiet\"."
  echo "      spike appears once more than SPIKE_THRESHOLD repeats arrive within SPIKE_WINDOW_MIN minutes."
fi
echo

if [ "$failed" -gt 0 ]; then
  echo "FAIL: $failed of $COUNT requests were rejected or got no answer."
  echo "      Is the hub running at $BASE, and is the ingest key right?"
  echo "      Last response: ${last:-<none>}"
  case "$last" in
    *signature*) echo "      That is the signature check: this hub has VERIFY_SIGNATURES on. Re-run with"
                 echo "      NIGHTJAR_SECRET=<the site's ingest_secret> set." ;;
  esac
  exit 1
elif [ "$distinct" -eq 1 ]; then
  echo "PASS: $COUNT crashes with different details collapsed into ONE issue."
  echo "      Without the fingerprint that would have been $COUNT rows, and $COUNT notifications."
else
  echo "FAIL: they landed in $distinct different issues, not one. The fingerprint is not"
  echo "      normalising something it should. Two hundred separate issues means it is broken."
  exit 1
fi

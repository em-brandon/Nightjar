#!/usr/bin/env bash
# Fire the same crash 200 times. You should end up with ONE issue.
# Usage: ./test_ingest.sh <ingest_key>
set -euo pipefail
KEY="${1:?pass the ingest key from seed.py}"
URL="http://localhost:8000/ingest/${KEY}/event"

for i in $(seq 1 200); do
  curl -s -X POST "$URL" -H 'Content-Type: application/json' -d "{
    \"exception_type\": \"OperationalError\",
    \"message\": \"connection to server at \\\"10.0.0.${i}\\\" failed after ${i} retries\",
    \"frames\": [
      {\"module\": \"db/session.py\", \"function\": \"connect\", \"lineno\": 41},
      {\"module\": \"routes/orders.py\", \"function\": \"list_orders\", \"lineno\": 88}
    ],
    \"level\": \"error\"
  }" > /dev/null
done

echo "sent 200. last response:"
curl -s -X POST "$URL" -H 'Content-Type: application/json' -d '{
  "exception_type": "OperationalError",
  "message": "connection to server at \"10.0.0.99\" failed after 3 retries",
  "frames": [
    {"module": "db/session.py", "function": "connect", "lineno": 41},
    {"module": "routes/orders.py", "function": "list_orders", "lineno": 88}
  ],
  "level": "error"
}' | python3 -m json.tool

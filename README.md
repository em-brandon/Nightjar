# incident hub — chunk 1: ingest

A stack trace goes in, a deduplicated issue comes out. No alerting yet.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # point DATABASE_URL at your postgres
python seed.py                # prints an ingest key and secret
uvicorn hub.main:app --reload
```

## Prove it works

```bash
./test_ingest.sh <ingest_key>
```

The last response should show `event_count` in the 200s with a single
`issue_id`, and `state_change: null` or `"spike"` — never 200 separate issues.
Note the varying IP and retry count in the payloads: the fingerprint normalises
those away, which is the entire point.

## What is deliberately missing

Alerting, Pushover, the console, email intake. Those are chunks 2–4. The one
thing to add before production is Alembic — `create_all` on startup is fine
while the schema is still moving, and a liability the moment it stops.

## Wiring up a client app

Copy `client/reporter.py` into any FastAPI app you manage and call
`install_reporter(app, url, secret)`. That is the whole integration.

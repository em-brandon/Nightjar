# Nightjar

Incident hub for a solo developer maintaining client sites alongside a day job.
Crashes and client reports go in; a small number of well-chosen phone
notifications come out.

**This file covers the software.** Hardware — the cabinet, wall display, Pico
companion — lives in `docs/FULL-DESIGN.md` and is not on the critical path.
`docs/DECISIONS.md` is the history: every choice, what was rejected, and why.
Read it before reversing anything here.

---

## The one thing that governs everything

The scarce resource is **attention at inconvenient hours**, not compute.

1. **A notification that did not need to be sent is a bug**, as serious as a
   missed one. If the operator mutes the app, the system has failed completely.
2. **The phone is the product.** The dashboard is for the morning triage pass.
3. **Nothing may suppress an alert.** Presence, fingerprint, the wall display,
   the speaker — each may *add* a channel or reveal detail. If all of them fail
   at once, Pushover still fires.

---

## Status

**Chunk 1: done.** Stack trace in, one deduplicated issue out. Verified: 200
crashes with varying IPs and retry counts collapse to a single row.

**Chunk 3: done.** React console, two read-only endpoints, 30-second polling.
Runs locally against the dev database; needs no paid service.

**Chunk 2: not written, deferred.** Nightjar therefore cannot alert anyone yet.
It is still the entire point of the project, but it needs a Pushover licence, and
production needs a domain and a VPS. Chunk 3 went first because it costs nothing.
Resume chunk 2 when those are paid for. Until then the console is a place to
*look*, not a reason to be interrupted, and nothing here should be mistaken for
monitoring.

Everything else waits.

---

## Architecture

```
client app crashes ─┐
uptime check fails  ─┼─→ ingest ─→ fingerprint ─→ alert policy ─┬─→ Pushover
client emails       ─┘                                          └─→ daily digest
```

One FastAPI service, one Postgres, one React console. Nothing else.

### Stack

| Piece | Role | Needed for chunk 2? |
|---|---|---|
| FastAPI | the hub | yes |
| PostgreSQL | issues, events, presence | yes |
| Pushover | push to iPhone | yes |
| React 19 + Vite | console | chunk 3 |
| Caddy | TLS, sole ingress | at deploy |
| Docker Compose | packaging | at deploy |

Local dev needs only the first three:

```bash
docker compose -f docker-compose.dev.yml up -d db
uvicorn hub.main:app --reload
```

`docker-compose.yml` is the production file and deliberately never publishes the
database port. The dev file does, on `127.0.0.1:5433` (not 5432, so it does not
collide with a Postgres already installed on the machine).

---

## Core concepts

### Fingerprint — `hub/fingerprint.py`

The hash that turns 1,284 identical exceptions into one row.

`site_id` + exception type + **normalised** message + innermost 3 frames as
`module:function`.

Deliberately excluded:

- **Line numbers** — they shift when you edit anything above the bug. A
  fingerprint that changes on reformat is worthless.
- **Per-occurrence values** — IPs, UUIDs, emails, quoted strings and numbers
  are normalised to placeholders before hashing.

Both failure directions are real. Too loose and unrelated bugs merge, so you
only hear about one. Too tight and every occurrence is its own row and the
console is unreadable.

### `state_change` — the seam between ingest and alerting

`POST /ingest/{key}/event` returns one of:

| Value | Meaning | Alerts? |
|---|---|---|
| `new` | first time this fingerprint has been seen | yes |
| `regression` | was resolved or snoozed, and is back | yes |
| `spike` | crossed `SPIKE_THRESHOLD` inside `SPIKE_WINDOW_MIN` | yes |
| `null` | known, already open, nothing changed | no |

**Alert on state change, not on events.** This single rule is the difference
between a system that gets used and one that gets muted in a week.

### Issue status

`open` · `resolved` · `muted` · `snoozed`

`muted` matters more than it looks. Some errors can't be fixed today — a flaky
third-party SDK, bots hitting a dead route. Muted issues keep counting but never
produce a `state_change`, including on regression. Without mute, noise poisons
the list and the operator stops trusting it.

### Severity — derived from source, never chosen by hand

| | Source | Night behaviour |
|---|---|---|
| P1 | uptime failure, `fatal` | wakes the phone, retries until acknowledged |
| P2 | `error`, rate spikes | normal push, quiet hours respected |
| P3 | client email, `warning` | digest only |

### Presence

Controls **which channels fire**, never **whether something alerts**.

Stored with an expiry, not as a toggle — a toggle desyncs permanently the first
time a tap is missed.

```python
class Presence(Base):
    state: Mapped[str]        # home | away
    source: Mapped[str]       # nfc | geofence | manual
    expires_at: Mapped[datetime]
```

An NFC tap sets `home` for 14 hours. On expiry, or with no record, the effective
state is **`away`** — the state where only the reliable channel is used. Always
fail toward the reliable channel.

### Routing matrix

| | P1 | P2 | P3 |
|---|---|---|---|
| Home, daytime | push + speaker | push + soft chime | digest |
| Home, night | priority-2 push + speaker | push only | digest |
| Away | priority-2 push | push | digest |

P2 is silent at night regardless of presence. Letting presence override quiet
hours reintroduces exactly what quiet hours exist to solve.

---

## Build order

**Chunk 1 — done.** Models, fingerprinting, ingest, HMAC verification, the
reporter snippet, the dedupe harness.

**Chunk 2 — next.** Alert policy, presence, Pushover dispatch.

- Policy reads `state_change`, `severity` and effective presence, applies quiet
  hours (22:00–07:00 Africa/Nairobi, P1 only), returns which channels to use.
- `POST https://api.pushover.net/1/messages.json` with `token`, `user`, `title`,
  `message`, `priority`, `url`, `url_title`.
- Priority 2 additionally needs `retry` (60), `expire` (1800), `sound`, and a
  `callback` URL. **Pushover POSTs that callback on acknowledgement, so no
  separate signed ack link is needed** — handle it at `POST /pushover/ack`.
- `POST /presence` accepts `{state, ttl_minutes, source, token}`, called by iOS
  Shortcuts NFC automations.
- Test by breaking a staging route, not with a unit test. Test presence by
  tapping out and confirming the push *still arrives*.

**Chunk 3 — done.** React console. One page, two endpoints
(`/api/sites/health`, `/api/issues`), 30-second polling, no websockets. Layout
reference in `mockups/nightjar-console.html`.

Built: verdict banner, site tiles, open-issue list, issue drawer. Site state is
*derived* from open issues (`down` = open P1, `warn` = open P2, else `up`), never
stored. When the hub is unreachable the console says so and never shows "All
quiet" — silence must not read as all-clear.

Not built, because the data does not exist yet: 90-day uptime ribbons and
"unacked" state (need an uptime probe and chunk 2's acknowledgement), and
Resolve / Mute / Snooze (need write endpoints — the natural first follow-up).
The mockup also shows deployments, client requests and the fingerprint reveal;
those belong to later chunks.

**Chunk 4.** Email intake (Postmark/Mailgun inbound → `/ingest/email`) and the
07:30 digest cron.

**Chunk 4b — fleet onboarding.** Only worth doing at the third or fourth app.
Before that, copying `reporter.py` is genuinely less work than maintaining a
published package.

Two halves, and the second matters more:

*Published reporters.* `@brandon/nightjar` on npm and `nightjar-reporter` on
PyPI, so a bug fix is `npm update` rather than editing N copies. Both read
`NIGHTJAR_URL` and `NIGHTJAR_SECRET` from the environment, so installing in a
new app is: install, set two vars, done.

The packaged reporter inherits the rule from `client/reporter.py` without
exception: **it must never break the app it reports on.** Wrapped and swallowed,
short timeout, no retry pile-up, no background queue that can grow unbounded.
A Nightjar outage must be invisible to a client's checkout page.

*Onboarding CLI.* `nightjar add-site <name> --client <client>` — creates the
site, prints the key and secret, optionally writes them into the app's `.env`.
Ten seconds instead of five minutes of fiddling, done N times.

The real friction at scale is not the reporter code — it is N key pairs, N sets
of environment variables, and N deploys to remember. Keep secrets in Gitea
Actions rather than in N `.env` files on N servers, so rotation is one edit and
not a hunt.

**Chunk 5+.** Wall display, Pico companion, CI integration, client status page.
All in `docs/FULL-DESIGN.md`.

**Before production, regardless of chunk:** replace `create_all` on startup with
Alembic. Fine while the schema moves; a liability the moment there is data worth
keeping.

---

## Repository layout

```
hub/
  config.py        pydantic-settings, reads .env
  db.py            async engine, session factory, Base
  models.py        Client, Site, Issue, Event
  fingerprint.py   normalise + hash — the load-bearing file
  schemas.py       request/response models
  ingest.py        POST /ingest/{key}/event
  main.py          app factory, lifespan, healthz
client/
  reporter.py      the whole "SDK" — drop into any managed FastAPI app
  thermal_probe.py host thermals as ordinary tickets
deploy/            bootstrap.sh, Caddyfile, backup.sh
mockups/           working HTML: console, wall, boot splash, drawer
docs/              FULL-DESIGN.md — hardware, access tiers, later chunks
```

`notify.py` (chunk 2) must stay a single adapter exposing one
`send(ticket, priority)` function. Nothing else should know which push provider
is in use — that is the swap point if Pushover is ever replaced.

---

## Out of scope

Named so they do not creep in: client portal with logins, SLA timers, comment
threads, multi-user auth, replying to clients from inside the app,
request-volume graphs, uptime percentage charts, a custom SDK per language,
Datadog, Jenkins.

Each is a real feature. Each would prevent this shipping this month.

Also out of scope: **rebuilding tools that exist.** Sentry or GlitchTip does
exception capture and webhooks in. Grafana reads the same Postgres for metrics.
Nightjar's job is narrow — deduplicate, decide, deliver.

---

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
docker compose -f docker-compose.dev.yml up -d db
python seed.py                # prints ingest_key and ingest_secret
uvicorn hub.main:app --reload
./test_ingest.sh <ingest_key>
```

The console, in a second terminal:

```bash
cd console && npm install && npm run dev    # http://localhost:5173
```

Vite proxies `/api` to the hub on `:8000`, so the hub needs no CORS config. After
`test_ingest.sh` the console should show one site in `warn` and one open issue
with a count in the 200s.

`test_ingest.sh` sends 200 copies of one crash with a different IP and retry
count each time. Correct output is a **single** `issue_id` with `event_count` in
the 200s. Two hundred separate issues means the fingerprint is broken.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` |
| `VERIFY_SIGNATURES` | HMAC on ingest. Off in dev, **on in prod** |
| `SPIKE_THRESHOLD` | events in the window that constitute a spike |
| `SPIKE_WINDOW_MIN` | the window, in minutes |

---

## Conventions

- Python 3.11+, async SQLAlchemy 2.0 with `Mapped[...]` annotations.
- **Reporting must never break the app it reports on.** Every outbound call in
  `client/reporter.py` is wrapped and swallowed.
- Ingest bursts serialise with `SELECT ... FOR UPDATE` on the issue row.
  Intentional: correctness over throughput at this scale.
- Comments explain *why*, not *what*. The fingerprint exclusions and the
  serialisation choice are where future-you will want the reasoning.

## Brand

Ink `#0F1420` · bone `#F2EFE6` · amber `#E8A33D`

Amber is reserved for alert state — P2 badges, warning dots, the status light,
the bird's eye. If amber appears on ordinary UI it stops meaning anything.

In the console, P1 is **red** and P2 is amber, as in the mockup: red means "wake
me", amber means "look soon". Making both amber would blur the one distinction
that matters most at 2am.

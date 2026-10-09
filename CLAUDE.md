# Nightjar

Incident hub for a solo developer maintaining client sites alongside a day job.
Crashes and client reports go in; a small number of well-chosen phone
notifications come out.

**This file covers the software.** Hardware — the cabinet, wall display, Pico
companion — lives in `docs/FULL-DESIGN.md` and is not on the critical path.
`docs/DECISIONS.md` is the history: every choice, what was rejected, and why.
Read it before reversing anything here. `docs/KEYS.md` explains every key, secret and token, with diagrams.

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

**Chunk 3: done, and grown.** React console with six tabs (Overview, Incidents,
Requests, Deploys, Staging, Media), an uptime probe, incident actions with an audit
trail, client-request tickets and CI deploy reports. 30-second polling. Runs
locally against the dev database; needs no paid service.

**Auth.** Everything under `/api` requires `CONSOLE_TOKEN`, as an HttpOnly session cookie or a
bearer token. Ingest keeps its per-site key and HMAC. With the token unset auth is off (local
development), and the hub **refuses to start** when `VERIFY_SIGNATURES` is on without one. The cookie
is only `Secure` over https, so run it behind Caddy's TLS. Still to do before internet exposure: the
console is not in the deploy stack yet.

**Chunk 2: not written, deferred.** Nightjar therefore cannot alert anyone yet.
It is still the entire point of the project, but it needs a Pushover licence, and
production needs a domain and a VPS. Chunk 3 went first because it costs nothing.
Resume chunk 2 when those are paid for. Until then the console is a place to
*look*, not a reason to be interrupted, and nothing here should be mistaken for
monitoring.

Everything else waits.

**Planned, not started:** moving the console to TypeScript, with API types generated from the hub's own schemas.
Plan and order in `docs/TYPESCRIPT-MIGRATION.md`. Until it lands, write new console files as `.ts` or `.tsx`.

**Planned, not started:** the client sites go behind Cloudflare's free plan, with **Caddy staying** as the web server (nginx
was considered and not needed, decision #53). The checklist and two small tested additions to the Caddyfile are in
`docs/CLOUDFLARE.md`. The hub's own hostname stays DNS-only, so a bot rule can never turn an error report into silence.

**Next up:** an onboarding screen in the console: add a site, see its keys once with a ready-made setup snippet for its
stack, and watch for its first report. The API half exists; the plan is in `docs/ONBOARDING-DASHBOARD.md`.

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

**Chunk 3 — done.** React console, 30-second polling, no websockets. Planned as
one page and two endpoints (`/api/sites/health`, `/api/issues`); it grew to five
tabs at the operator's request (see `docs/DECISIONS.md` #28). Layout reference in
`mockups/nightjar-console.html`.

Built: verdict, odometer stat strip, per-site check cards (uptime ring, latency,
90-day ribbon), a tile per area, and the five tabs. Site state is *derived*, never
stored: `down` = an open P1 or two failed probes in a row, `warn` = an open P2,
one failed probe or a silent probe, else `up`. A probe that has gone quiet is
never `up`. When the hub is unreachable the console says so on every tab and never
shows "All quiet": silence must not read as all-clear.

Data flows, all real tables:

| Feature | In | Out |
|---|---|---|
| Uptime checks | `hub/probe.py` requests each `Site.url` every `PROBE_INTERVAL_S` | `/api/sites/health` |
| Errors rising | derived from `events` (last 30 min > 2x the 30 before) | `/api/errors/rising` |
| Incident actions | `POST /api/issues/{id}/actions` (ack, resolve, mute, snooze, reopen) | `/api/issues/{id}/log` |
| Client requests | `POST /api/tickets` now, email intake in chunk 4 | `/api/tickets` |
| Deploys | CI calls `POST /ingest/{key}/deploy` (same signing as crashes). A running report may carry `progress` 0 to 100, shown as a card under the wall's clock (loading, done, complete, failed) while a staging push runs | `/api/deploys` |
| Staging | just a site with `env=staging` and a `url` | same as production |
| Stacks | `client/stack_reporter.py` on the staging host *pushes* to `POST /ingest/{key}/stack` | `/api/stacks` |
| Stack problems | degraded, restart loop, memory over 85%, host disk under 10% | an issue, **edge-triggered** (only when it starts), which **resolves itself** after 10 healthy minutes |
| Home host | `client/home_reporter.py` on the ThinkCentre *pushes* CPU temperature, disk, memory, backup age and an hourly internet speed test to `POST /ingest/{key}/home` | `/api/home`, a dashboard tile and the idle screen's header icons (with live numbers and hover tooltips) |
| Server errors, any stack | `client/reporter.py` (FastAPI), `client/node`, `client/php`, `client/java`, signed with the site's ingest secret | `POST /ingest/{key}/event`. The same contract in every language (`docs/REPORTERS.md`), proven by `tests/test_server_reporters.py` |
| Browser errors | `client/browser/nightjar.ts` in a site's page, with the site's *public* key | `POST /ingest/browser/{public_key}`: no HMAC, so Origin-checked, rate limited, level clamped (never P1) and capped at 10 new issues an hour per site. Same issues list |
| Home media | `client/jellyfin_reporter.py` on the ThinkCentre *pushes* Jellyfin's sessions to `POST /ingest/{key}/media` | `/api/media`, `/api/media/history`, the Media tab and an idle-screen panel |

**Staging promotion.** A staging site is paired with its production site by *name*: `<production>-staging`
belongs to `<production>`. `console/src/promotion.js` decides ready / blocked / in sync / behind. Blockers
(failed or unfinished deploy, uptime under 99%, open P1/P2, silent, degraded or restarting stack) always win.

**A stop is not a crash.** In `client/stack_reporter.py`, exit codes 0, 137, 143 and 255 mean stopped
(Docker Desktop's VM, a reboot, `compose stop`), never a failure. A real crash code is unhealthy for 15
minutes, then stopped, and an out-of-memory kill always counts. Do not collapse these back into "non-zero
means broken": it raised a false alarm on every Docker Desktop restart.

**Auto-resolve is strict.** A stack problem resolves only after `STACK_HEALTHY_MIN` minutes with no
recurrence, history covering the whole window, and no reporter gap (silence is not health). Only `open`
issues are touched, and it is logged as automatic. See `_healthy_for` and `tests/test_stack_policy.py`.

**Stack problems are events, and must be edge-triggered.** `hub/stacks.py` opens an issue only when a condition
*starts*, through `record_event` (extracted from ingest so every source shares one fingerprinting path). Never
emit one per report: 1,440 a day would trip the spike detector.

**Nightjar shows staging stacks; it never controls them.** The hub is on the VPS and staging is on a
machine at home, so it cannot and must not reach that Docker. A reporter there pushes state in; start,
stop and logs live in Dockge over Tailscale. A reporter that has gone quiet is shown as `reporter
silent`, never as healthy.

A site with no `url` is not monitored: it still reports crashes but has no ring
or ribbon. Sites are created with `POST /api/sites` (console token; returns the ingest key and secret once)
and `PATCH /api/sites/{id}` sets or clears the `url`. No SQL. A staging site must be named
`<production>-staging`, which is how promotion pairs them. `seed.py` remains for the first demo site.

The **idle screen** (`#/idle`, `console/src/views/Idle.jsx`) starts by itself after 5
minutes with no input and doubles as the wall display. Two production sites at a
time, rotating every 2 minutes, plus active incidents and production and staging
deploys. It hides detail, not names: incident titles and commit refs appear only
after a fingerprint reveal (2 minutes, then it reverts by itself). Only reveal
then *Unlock console* leaves it; stray input never does. The fingerprint is
**simulated** until the R307 reader exists (chunk 6): `useReveal()` in `hooks.js`
is the one seam to swap. Unlock only navigates, and gives no real protection until
the write endpoints are gated (#32).

Still not built: a real ack from the phone (needs chunk 2's Pushover callback), the
alert-sent lines in the incident log, and the mockup's fingerprint reveal.

**Chunk 4.** Email intake (Postmark/Mailgun inbound → `/ingest/email`) and the
07:30 digest cron.

**Chunk 4b — fleet onboarding.** Only worth doing at the third or fourth app.
Before that, copying `reporter.py` is genuinely less work than maintaining a
published package.

Two halves, and the second matters more:

*Published reporters.* `@brandon/nightjar` on npm (**the scope is unverified and plain
`nightjar` is taken**: see `docs/CLIENT-INTEGRATION.md`) and `nightjar-reporter` on
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

**Expanded plan:** `docs/CLIENT-INTEGRATION.md` adds push checks (results from a probe inside a
private network), heartbeats (alert on silence), an add-site form, and a `nightjar` CLI plus a
dependency-free `nightjar.sh`. Two rules for any of it: the package runs with a client's site
secret, so it has **zero runtime dependencies** and is published with provenance; and CI or cron
holds **ingest-scoped credentials only**. The console token never goes in a client's CI.

The real friction at scale is not the reporter code — it is N key pairs, N sets
of environment variables, and N deploys to remember. Keep secrets in Gitea
Actions rather than in N `.env` files on N servers, so rotation is one edit and
not a hunt.

**Chunk 5+.** Wall display, Pico companion, CI integration, client status page.
All in `docs/FULL-DESIGN.md`.

**Schema changes go through Alembic.** The hub migrates on every start. Edit `hub/models.py`, run
`python -m alembic revision --autogenerate -m "..."`, read the file it writes, commit it. `python -m alembic
check` must pass. Never add a column by hand: `tests/test_migrations.py` exists to catch a model changed
without a migration. A pre-migration database is adopted only if it matches the models.

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
  browser.py       POST /ingest/browser/{public_key}: errors from web pages, no secret, so bounded (docs/KEYS.md)
  main.py          app factory, lifespan (starts the probe), healthz
  api.py           read-only console feeds: issues, site health, rising errors
  probe.py         uptime checker loop, writes checks
  incidents.py     issue actions + audit log
  sites.py         create a site, set its url: POST/PATCH /api/sites
  tickets.py       client requests
  deploys.py       CI deploy reports
  stacks.py        staging stack reports in, /api/stacks out
  media.py         Jellyfin snapshots in, /api/media and viewing history out
  weather.py       /api/weather: cached Open-Meteo reading, rain, UV and pressure for the wall (decoration only)
  home.py          home host snapshots and internet speed tests in, /api/home out
  presence.py      home or away as taps with an expiry (away on expiry or with no record)
  rates.py         /api/rates: cached USD to KES rate (decoration only)
  edge.py          edge-triggered issues shared by every pushed source: open once, resolve themselves
console/           Vite + React 19 UI: src/views/* is one file per tab
client/
  reporter.py      the whole "SDK" — drop into any managed FastAPI app
  browser/nightjar.ts  the browser reporter: one dependency-free file, public key only (docs/KEYS.md)
  node/nightjar.ts, php/Nightjar.php, java/Nightjar.java  server reporters: dependency-free, signed with the ingest secret (docs/REPORTERS.md)
  thermal_probe.py host thermals as ordinary tickets
  stack_reporter.py runs on the staging host, pushes Docker stack state to the hub
  jellyfin_reporter.py runs beside Jellyfin, pushes who is streaming what to the hub
  home_reporter.py runs on the ThinkCentre, pushes its health and an hourly speed test to the hub
deploy/            bootstrap.sh, Caddyfile, backup.sh
  staging/         Dockge (Tailscale-only), reporter timer, client stack template
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

**Media management.** Two separate things. The film archive on Raspberry Pis (`docs/MEDIA-ARCHIVE.md`) is
planned and decoupled from the ThinkCentre. Home streaming (Jellyfin) is built: it runs on the ThinkCentre and
Nightjar watches it through a reporter (Media tab, idle panel, viewing history kept for `MEDIA_HISTORY_DAYS`).
Nightjar never stores, transcodes, browses or plays footage. Media problems are P2 or P3, never P1, and
production runs on the VPS, so media can slow staging but never a client's live site. On the idle screen,
who is watching and what stays behind the fingerprint reveal.

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
npm test                                    # promotion-readiness rules + CSS collision guard (console/src/*.test.mjs)
python -m unittest discover -s tests        # migrations, auth, stack policy (needs the dev db up)
python -m unittest discover -s client       # the stack reporter
```

Vite proxies `/api` to the hub on `:8000`, so the hub needs no CORS config. After
`test_ingest.sh` the console should show one site in `warn` and one open issue,
its count growing by 200 each run.

`test_ingest.sh` sends 200 copies of one crash with a different IP and retry
count each time (the 200 is only how many copies it sends, not a limit). It counts
the distinct issue ids that come back and prints **PASS** only if there is exactly
one (exit 0); otherwise it prints **FAIL** and why (exit 1). More than one issue
means the fingerprint is broken. Set `NIGHTJAR_URL` and `NIGHTJAR_SECRET` to run it
signed against a hub with `VERIFY_SIGNATURES` on.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` |
| `VERIFY_SIGNATURES` | HMAC on ingest. Off in dev, **on in prod** |
| `SPIKE_THRESHOLD` | events in the window that constitute a spike |
| `SPIKE_WINDOW_MIN` | the window, in minutes |
| `PROBE_ENABLED` | run the uptime checker (default true) |
| `PROBE_INTERVAL_S` | seconds between rounds (default 60) |
| `PROBE_TIMEOUT_S` | per-request timeout (default 10) |
| `CONSOLE_TOKEN` | guards the console and `/api`. Empty = open (dev only). Required in production, min 16 chars |
| `STACK_HEALTHY_MIN` | minutes a stack problem must stay gone before it resolves itself (default 10) |
| `MEDIA_HISTORY_DAYS` | how long the home media viewing history (personal data) is kept (default 365) |
| `WEATHER_ENABLED` / `WEATHER_LAT` / `WEATHER_LON` / `WEATHER_NAME` / `WEATHER_TTL_MIN` | the wall's weather and altitude, from Open-Meteo (no key). Defaults to Nairobi; set the exact coordinates of the wall, since the elevation shown is the terrain model's value at that point |
| `BROWSER_RATE_PER_KEY_MIN` / `BROWSER_RATE_PER_IP_MIN` / `BROWSER_NEW_ISSUES_PER_HOUR` | brakes on browser reporting: 120 and 20 reports a minute, and 10 new issues an hour per site |
| `PRESENCE_TOKEN` | shared secret for the NFC presence tap (`POST /presence`). Empty disables that route |
| `RATES_ENABLED` / `RATES_TTL_MIN` | the USD to KES rate on the wall, from open.er-api.com (no key) |
| `VITE_STACK_CONTROL_URL` | console build-time: where the Staging tab's *Manage stacks* button points (Dockge over Tailscale). Optional |

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

Ink `#0A0A0B` · bone `#F2EFE6` · amber `#E8A33D`

The console is neutral near-black with no blue cast (surfaces `#131315`, `#1D1D20`, hairlines `#232326`). The mockups still use the original navy `#0F1420`; they are design references, not the implementation.

Amber is reserved for alert state — P2 badges, warning dots, the status light,
the bird's eye. If amber appears on ordinary UI it stops meaning anything.

The idle screen's panel headings are the one deliberate exception to the neutral palette, chosen by the
operator: production deploys orange, staging deploys green, home media blue, active incidents yellow with a
slow pulse (`--orange`, `--green`, `--blue`, `--yellow`), each with a local `lucide-react` icon. They colour
the heading only. Orange and yellow are kept visibly apart from the amber that means P2. Hidden detail on
that screen is blurred, never replaced with dots. Errors rising is red (green belongs to staging deploys),
with a red X when nothing is climbing. Client requests are deliberately not a card: an inbox icon and one
number in the incidents heading, amber and breathing only when a request is overdue.

In the console, P1 is **red** and P2 is amber, as in the mockup: red means "wake
me", amber means "look soon". Making both amber would blur the one distinction
that matters most at 2am.

# Nightjar

**An incident hub for one developer who looks after client sites alongside a day job.**
Crashes, uptime failures and client requests go in. A small number of well-chosen phone notifications come out.

Named for a bird that only becomes active after dark, which is when this system will contact you.

---

## Table of contents

- [Status at a glance](#status-at-a-glance)
- [Why Nightjar exists](#why-nightjar-exists)
- [How it works](#how-it-works)
- [The stack](#the-stack)
- [Core concepts](#core-concepts)
  - [Fingerprint](#fingerprint)
  - [state_change](#state_change)
  - [Issue status](#issue-status)
  - [Severity](#severity)
  - [Presence and routing](#presence-and-routing)
  - [Presence and NFC tags](#presence-and-nfc-tags)
- [The console](#the-console)
- [The wall monitor](#the-wall-monitor)
  - [What the monitor shows](#what-the-monitor-shows)
  - [Access tiers](#access-tiers)
  - [The cabinet](#the-cabinet)
  - [Airflow and thermals](#airflow-and-thermals)
  - [The Pico companion](#the-pico-companion)
  - [The dead-man's switch](#the-dead-mans-switch)
- [Where things run](#where-things-run)
- [API reference](#api-reference)
- [Quick start](#quick-start)
  - [Configuration](#configuration)
  - [Prove it works](#prove-it-works)
- [Wiring up apps](#wiring-up-apps)
- [Deploying](#deploying)
- [Roadmap](#roadmap)
- [Known gaps](#known-gaps)
- [Conventions](#conventions)
- [Out of scope](#out-of-scope)
- [Documentation map](#documentation-map)
- [Brand](#brand)

---

## Status at a glance

| Piece | State |
|---|---|
| Ingest, fingerprinting, deduplication (chunk 1) | **Done and tested.** 200 varied crashes collapse to one issue |
| Console: five tabs, uptime probe, incident actions, tickets, deploys, idle screen (chunk 3) | **Done.** Runs locally against a dev database |
| Alerting: alert policy, presence, Pushover, speaker (chunk 2) | **Not written.** Deferred until the Pushover licence, a domain and a VPS are paid for |
| Email intake and the 07:30 digest (chunk 4) | Not started |
| Wall display route, Pico companion, Gitea integration, client status page (chunks 5 to 8) | Designed, not built. The idle screen is the software precursor to the wall display |

> **Nightjar cannot yet interrupt you.** Until chunk 2 exists it deduplicates crashes and shows them
> in the console, but nothing buzzes your phone. Do not rely on it to catch a client outage.

---

## Why Nightjar exists

The operator has a day job. The scarce resource is not compute or storage, it is **attention at
inconvenient hours**. Every design decision is downstream of that.

1. **A notification that did not need to be sent is a bug**, as serious as a missed one. If the
   operator mutes the app, the system has failed completely.
2. **The phone is the product.** The console is for the morning triage pass, not for staring at.
3. **Nothing may suppress an alert.** Presence, fingerprint, the wall monitor, the speaker: each may
   *add* a channel or reveal detail. If all of them fail at once, Pushover still fires.

---

## How it works

```
client app crashes ─┐
uptime check fails  ─┼─→ ingest ─→ fingerprint ─→ alert policy ─┬─→ Pushover (P1 retries until acknowledged)
client emails       ─┘                                          └─→ daily digest
```

One FastAPI service, one Postgres, one React console. Everything else is a client of them.

What Nightjar deliberately does **not** do:

- **Exception capture.** Sentry or GlitchTip do it properly and forward in by webhook. Nightjar owns
  the deduplicated ticket store and the decision of whether to buzz the phone.
- **A native iOS app.** Pushover is the iOS app. Native means a developer account, APNs certificates
  and a Swift codebase, all to deliver a push notification.
- **Client logins.** Clients get an email address. A read-only status page comes later, when one
  asks twice.
- **Share a failure domain with what it watches.** It lives on a VPS, never on client
  infrastructure and never on the machine at home.

---

## The stack

| Layer | Technology | Role |
|---|---|---|
| Hub | FastAPI, Python 3.11+ | Ingest, fingerprinting, API, uptime probe |
| Database | PostgreSQL 16, async SQLAlchemy 2.0 | Issues, events, checks, tickets, deploys |
| Console | React 19, Vite 6 | Triage UI and the idle screen |
| Push | Pushover | iPhone delivery. Priority 2 retries every 60 s for 30 min until acknowledged |
| Ingress | Caddy 2 | TLS and the only public entry point |
| Packaging | Docker Compose | One `docker compose up` on the VPS |
| OS | Ubuntu Server LTS | Everywhere. Not Windows Server |

Around it, as separate services that feed Nightjar rather than parts of it:

| Service | Role |
|---|---|
| Sentry or GlitchTip | Optional richer error capture, forwarded by webhook |
| Uptime Kuma | A **second** probe from a different network, so "the client is down" is distinguishable from "my ISP is down" |
| Gitea and Gitea Actions | Self-hosted Git and CI. Deploy status reports back into Nightjar |
| Grafana | Reads the same Postgres for metrics. Not rebuilt inside Nightjar |
| Cloudflare Email Routing, Postmark or Mailgun | Inbound client email, parsed into tickets |
| healthchecks.io or Cronitor | External heartbeat: alerts you if Nightjar itself goes quiet |

Rejected on purpose, with reasons in [`docs/DECISIONS.md`](docs/DECISIONS.md): Datadog, Jenkins, a
native app, a client portal, running a mail server, hosting the hub at home.

---

## Core concepts

### Fingerprint

`hub/fingerprint.py` is the hash that turns 1,284 identical exceptions into one row.

It is built from `site_id` + exception type + **normalised** message + the innermost three frames as
`module:function`. Deliberately excluded:

- **Line numbers.** They shift when you edit anything above the bug. A fingerprint that changes on
  reformat is worthless.
- **Per-occurrence values.** IPs, UUIDs, emails, quoted strings and numbers become placeholders
  before hashing.

Both failure directions are real. Too loose and unrelated bugs merge, so you only hear about one.
Too tight and every occurrence is its own row.

### state_change

The seam between ingest and alerting. `POST /ingest/{key}/event` returns one of:

| Value | Meaning | Alerts? |
|---|---|---|
| `new` | first time this fingerprint has been seen | yes |
| `regression` | was resolved or snoozed, and is back | yes |
| `spike` | crossed `SPIKE_THRESHOLD` inside `SPIKE_WINDOW_MIN` | yes |
| `null` | known, already open, nothing changed | no |

**Alert on state change, not on events.** This one rule is the difference between a system that gets
used and one that gets muted in a week.

### Issue status

`open` · `resolved` · `muted` · `snoozed`

`muted` matters more than it looks. Some errors cannot be fixed today: a flaky third-party SDK, bots
hitting a dead route. Muted issues keep counting but never produce a `state_change`, including on
regression.

### Severity

Derived from the source, never chosen by hand.

| | Source | Night behaviour |
|---|---|---|
| **P1** | uptime failure, `fatal` | wakes the phone, retries until acknowledged |
| **P2** | `error`, rate spikes | normal push, quiet hours respected |
| **P3** | client email, `warning` | digest only |

### Presence and routing

Presence controls **which channels fire**, never **whether something alerts**. It is stored with an
expiry, not as a toggle, because a toggle desyncs permanently the first time a tap is missed.

An NFC tap sets `home` for 14 hours. On expiry, or with no record, the effective state is **`away`**,
the state where only the reliable channel is used. Always fail toward the reliable channel.

| | P1 | P2 | P3 |
|---|---|---|---|
| Home, daytime | push + speaker | push + soft chime | digest |
| Home, night | priority-2 push + speaker | push only | digest |
| Away | priority-2 push | push | digest |

Quiet hours are 22:00 to 07:00 Africa/Nairobi. P2 is silent at night regardless of presence, because
letting presence override quiet hours brings back exactly what quiet hours exist to solve.

The speaker is rate-limited from day one: one sound per issue per 15 minutes and a global hourly cap.
Without both, a spike machine-guns the speaker and it gets unplugged inside a week.

> Presence, routing and the speaker are chunk 2 and are **not implemented yet**.

### Presence and NFC tags

Presence is how Nightjar knows whether you are home, so it can add the speaker or hold a chime. It is
set by tapping an NFC tag with your iPhone.

> **Planned, not built.** This is chunk 2. Nothing below exists in the code yet.

**The tags.** Two NTAG213 stickers, because one tag maps to exactly one iOS Shortcuts automation:

| Tag | Where | Sets |
|---|---|---|
| `arriving` | inside the door frame | `home`, with a 14-hour expiry |
| `leaving` | by the keys | `away` |

Create one Shortcuts personal automation per tag and switch **off** "Ask Before Running" on both.
Otherwise every tap waits for a confirmation, and you will stop tapping.

**The endpoint.** Each automation calls the hub:

```
POST /presence
{"state": "home", "ttl_minutes": 840, "source": "nfc", "token": "<shared secret>"}
```

`source` is `nfc`, `geofence` or `manual`. `token` is how the hub knows the call is yours.

**Inputs, in order of trust:**

1. **NFC tap.** Explicit, so the most trusted.
2. **Geofence automation.** Catches a forgotten tap. It is flaky and can arrive late, which is harmless
   because every state expires.
3. **Manual override** in the console.

**Why it expires.** A stored toggle desyncs permanently the first time a tap is missed. An expiry lapses on
its own, and with no record the state is `away`, the state where only the reliable channel (the push) is
used. A missed tap therefore costs you a speaker announcement, never an alert.

**How to test it.** Tap out, then break a staging route and confirm the push **still arrives**. Presence
may add a channel. It must never be the reason a push does not fire.

---

## The console

A React app in [`console/`](console/), polling the hub every 30 seconds. No websockets. Dark only, in
neutral near-black.

The left sidebar has five tabs, with live count badges:

| Tab | What it does |
|---|---|
| **Overview** | Verdict line, odometer stats, a health card per production site (uptime ring, latency, 90-day ribbon), and a tile per area |
| **Incidents** | Open and all issues. A drawer with Acknowledge, Resolve, Mute, Snooze and Reopen, and an audit-trail log |
| **Requests** | Client requests (change, report, access) with overdue flagging. Logged by hand until email intake exists |
| **Deploys** | Production deploy history, reported by CI |
| **Staging** | Staging sites (a site with `env=staging`) with the same checks, plus staging deploys |

How the numbers are derived, so nothing is invented:

- **Site state is derived, never stored.** `down` is an open P1 or two failed probes in a row. `warn` is an
  open P2, one failed probe, or a probe that has gone quiet. Otherwise `up`. A silent probe is never `up`.
- **The 90-day ribbon** colours each day by its worst state. Days with no checks are grey, so "no data"
  never reads as "all good".
- **Errors rising** lists open issues whose event rate over the last 30 minutes is more than double the
  30 minutes before.
- **When the hub is unreachable** the console says so on every tab and never shows "All quiet".
  Silence must not read as all-clear.

Design references live in [`mockups/`](mockups/) as working HTML.

---

## The wall monitor

A dedicated monitor on the wall, driven by the ThinkCentre, showing the state of every site across the
room. It is a display, never a decision-maker: it may add detail, but it can never be the reason a push
does not fire.

### What the monitor shows

Two tiers of information, with the sensitive part hidden until you prove you are present:

- **Ambient (always on):** the verdict line, site names, severity, uptime and deploy status.
- **Revealed (fingerprint, 2 minutes):** incident titles, counts, ages and commit refs. It reverts by
  itself, never by a button, because you will walk away without pressing anything.

What is redacted is **detail, not names**. A site name tells a guest little, but
`postgresql://app@10.0.0.14:5432/sacco` in a stack trace tells them a great deal.

**What exists today** is the idle screen, the software precursor to the wall display. Open it at
`#/idle` in the console, or leave the console alone for 5 minutes and it starts by itself.

- Two production sites at a time as large check cards. It rotates to the next pair every 2 minutes.
- Active incidents, production deploys and staging deploys, plus a large clock.
- Incident titles and commit refs stay hidden until revealed.
- Ordinary mouse or keyboard input never dismisses it. Only reveal, then **Unlock console**, leaves it.
- The fingerprint is **simulated** with a labelled button until the reader hardware exists. Unlock only
  navigates and is not a security boundary (see [Known gaps](#known-gaps)).

**What is planned** (chunk 5): a `GET /wall` route that answers `404` to anything but the machine itself
(`127.0.0.1`), plus a single-use enrollment cookie. No token ever goes in the URL, because it lands in
browser history and access logs. It runs as a `cage` (Wayland kiosk) plus Chromium unit under systemd
with `Restart=always`, blanking disabled with `consoleblank=0`, and it caches the last good response so
an ISP blip shows stale data rather than an error.

### Access tiers

Three levels, each with its own threat model. Two are conveniences and only one is authentication.

| Tier | Surface | Gate | Shows |
|---|---|---|---|
| 1 Ambient | Wall monitor | none | Verdict, site names, uptime ribbons, percentages |
| 2 Limited | Wall monitor | R307 fingerprint, 2 minutes | Titles, counts, ages. Nothing diagnostic |
| 3 Full | Console | Passkey (WebAuthn) | Stack traces, variables, resolve, mute, snooze |

The R307 matches on the module and returns only a template ID, so the fingerprint itself never travels
or is stored. It is **convenience-grade**: fine for unblurring a screen in your own house, unacceptable
for anything that changes state.

### The cabinet

The hardware is a **Lenovo ThinkCentre M920q** (i7-8700T, 6 cores, 35 W, 8 GB plus a 16 GB SODIMM),
mounted vertically in a wall cabinet. It runs staging, Gitea, builds, backups, the second uptime probe,
and the wall display. It does **not** run Nightjar itself, which stays on the VPS.

The drawings below are in [`assets/`](assets/) and are generated by
[`tools/make_case_drawings.py`](tools/make_case_drawings.py).

**Isometric cutaway.** Two device bays side by side, a 40 mm rear plenum, filtered fans in the floor and
lid, and HDMI going up to the screen.

![Cabinet isometric cutaway](assets/case-isometric.svg)

**Plan, side section and rear elevation.** The cabinet is 400 × 250 × 120 mm internally. Air enters
through the floor and leaves through the lid, so convection helps the fans rather than fighting them.

![Cabinet plan, side section and rear elevation](assets/case-views.svg)

**Front panel cutouts.** A 1.5 mm steel plate, 280 × 360 mm, with an intake perforation, an acrylic
sub-panel and the RJ45 and USB cutouts.

![Front panel cutout detail](assets/front-panel-detail.svg)

**Wall mount.** Everything bolts to a shared 3 mm backplate on 40 to 50 mm standoffs. Nothing is drilled
into the ThinkCentre, and it is never mounted inverted.

![Wall mount layout](assets/mount-layout.svg)

> **These drawings predate several decisions. Treat them as reference, not as a cutting list.**
>
> - They show a **2.42" OLED and touch pad**. The OLED was later dropped in favour of the wall monitor
>   (decision #19).
> - They are dimensioned for an **M720q, 34.5 mm thick**. The chosen machine is the M920q at **37 mm**.
> - They show a perforated steel front. The later design splits the front into glass above and a solid
>   band below, with a roughly 35 mm plenum fed from below.
> - Connector footprints vary by manufacturer: buy the RJ45 and USB parts, measure them, then release the
>   panel to cut.

### Airflow and thermals

Lenovo's manual does not document airflow direction. It was read from chassis photos: **intake** is the
hex mesh at the bottom of the front bezel, **exhaust** is the rear slot column and the top face. So a glass
door cannot sit flush against the front, because that mesh is the intake.

That is still a strong hypothesis, not a measurement. **Run the tissue test on every face under load
before cutting steel.**

Acceptance targets for any enclosure:

| Measure | Target |
|---|---|
| Intake air vs room ambient | within 5 °C |
| CPU package, sustained load | under 80 °C |
| NVMe composite | under 65 °C |
| Throttle events over 30 minutes | zero |

Take an **open-air baseline before the cabinet exists**: it cannot be recovered afterwards.
[`client/thermal_probe.py`](client/thermal_probe.py) reports breaches into Nightjar as ordinary tickets.

### The Pico companion

A small Raspberry Pi Pico beside the monitor (chunk 6). Its jobs are a WS2812 status strip, the R307
fingerprint reader and a heartbeat watchdog. The RP2040 draws about 0.3 W, so it has no fan. The serial
adapter must be a no-op when nothing is attached.

Its watchdog is **deliberately dumb**: Nightjar pulses every 10 seconds, and after 60 seconds of silence
the strip blinks slow white on its own. It knows nothing about the sites. A smarter watchdog would need
to know what healthy looks like, and could therefore be wrong about it.

### The dead-man's switch

Both the Pushover call and the speaker are driven by the hub, so they share its fate. If the hub dies it
does not complain: it goes **silent**, and silence is indistinguishable from everything being fine.

Nightjar therefore pings an external heartbeat service every 5 minutes. If the pings stop, *they* alert
your phone. This is the only mechanism that turns silence back into a signal, and it is the reason
self-hosting is defensible at all.

---

## Where things run

Two machines, split by consequence. **Nightjar lives on the VPS, permanently.** That is not a stopgap.

| VPS (Ubuntu LTS, 2 vCPU / 4 GB) | ThinkCentre M920q (Ubuntu LTS, headless) |
|---|---|
| Nightjar hub | Staging environments, one stack per client |
| Postgres | Gitea and Gitea Actions |
| Caddy, the only ingress | Grafana |
| External heartbeat ping | Uptime Kuma, the **second** probe |
| | Backup target, pulling nightly from the VPS |
| | Chromium kiosk driving the wall monitor |

The rule: **Nightjar must be more reliable than the things it watches.** A watchdog at home inherits the
house's ISP, power and maintenance mistakes, and when it dies it goes silent. The wall monitor runs *on*
the ThinkCentre but fetches from the VPS over HTTPS, so switching the screen off or losing home internet
does not affect alerting at all.

---

## API reference

Ingest, called by client apps and CI. Authenticated by the per-site key in the URL. With
`VERIFY_SIGNATURES=true`, also by an `X-Hub-Signature: sha256=<hmac-sha256 of the raw body>` header.

| Method and path | Purpose |
|---|---|
| `POST /ingest/{key}/event` | Report a crash. Returns `issue_id`, `fingerprint`, `event_count`, `severity`, `state_change` |
| `POST /ingest/{key}/deploy` | Report a deploy: `{"ref": "main@1f9c4a2", "status": "running \| success \| failed"}` |

Console feeds, read-only:

| Method and path | Purpose |
|---|---|
| `GET /api/sites/health` | Per-site state, uptime, latency, latency series and 90-day ribbon |
| `GET /api/issues?status=open\|all` | Issues, open first, then by severity and recency |
| `GET /api/errors/rising` | Open issues whose event rate is climbing |
| `GET /api/issues/{id}/log` | The audit trail for one issue |
| `GET /api/tickets?status=open\|closed\|all` | Client requests |
| `GET /api/deploys?env=production\|staging\|all` | Deploy history |
| `GET /healthz` | Liveness |

Console writes. **These have no authentication yet** (see [Known gaps](#known-gaps)):

| Method and path | Purpose |
|---|---|
| `POST /api/issues/{id}/actions` | `{"action": "ack \| resolve \| mute \| snooze \| reopen", "hours": 24}` |
| `POST /api/tickets` | Log a client request |
| `PATCH /api/tickets/{id}` | Close or reopen one |

Planned: `POST /pushover/ack` and `POST /presence` (chunk 2), `POST /ingest/email` (chunk 4), `GET /wall`
(chunk 5), `POST /ingest/gitea` (chunk 7).

---

## Quick start

Requires Python 3.11+, Node 20+ and Docker. No paid service is needed to run it locally.

```bash
git clone https://github.com/em-brandon/Nightjar.git && cd Nightjar

python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

docker compose -f docker-compose.dev.yml up -d db    # Postgres on 127.0.0.1:5433
python seed.py                                       # prints ingest_key and ingest_secret
uvicorn hub.main:app --reload                        # http://localhost:8000
```

In a second terminal:

```bash
cd console && npm install && npm run dev             # http://localhost:5173
```

Vite proxies `/api` to the hub, so there is no CORS to configure. The dev database is a **separate
compose file** on purpose: `docker-compose.yml` is the production file and never publishes the database
port. The dev one publishes `5433`, not `5432`, so it cannot collide with a Postgres already installed on
the machine.

To give a site uptime checks, set its URL. It is probed every minute and appears on the Overview:

```bash
docker compose -f docker-compose.dev.yml exec db \
  psql -U hub -d hub -c "UPDATE sites SET url = 'https://client.example/healthz' WHERE name = 'demo-api';"
```

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `.env.example` sets `...@localhost:5433/hub` | Database connection. The built-in fallback is port 5432 |
| `VERIFY_SIGNATURES` | `false` | HMAC check on ingest. Off in dev, **on in prod** |
| `SPIKE_THRESHOLD` | `50` | Events inside the window that count as a spike |
| `SPIKE_WINDOW_MIN` | `10` | The window, in minutes |
| `PROBE_ENABLED` | `true` | Run the uptime checker |
| `PROBE_INTERVAL_S` | `60` | Seconds between probe rounds |
| `PROBE_TIMEOUT_S` | `10` | Per-request timeout |

### Prove it works

```bash
./test_ingest.sh <ingest_key>
```

It sends 200 copies of one crash, each with a different IP address and retry count. Correct output is a
**single** `issue_id` with `event_count` in the 200s. Two hundred separate issues means the fingerprint is
broken. Open the console afterwards: one site in `warn` and one open issue.

---

## Wiring up apps

**Crash reports.** Copy [`client/reporter.py`](client/reporter.py) into any FastAPI app you manage and call
`install_reporter(app, url, secret)`. That is the whole integration. It must never break the app it
reports on: every outbound call is wrapped and swallowed, with a short timeout and no retry pile-up.

**Deploys.** Add one step to the end of a pipeline, using the site's ingest key:

```bash
curl -s -X POST "$NIGHTJAR_URL/ingest/$NIGHTJAR_KEY/deploy" \
  -H "Content-Type: application/json" \
  -d '{"ref": "'"$GITHUB_REF_NAME@$GITHUB_SHA"'", "status": "success"}'
```

Send `running` when a deploy starts, then `success` or `failed`. A staging environment is just a site with
`env` set to `staging`, so it uses its own key. See [`INTEGRATE.md`](INTEGRATE.md) for Sentry, uptime
monitors and Gitea.

---

## Deploying

Nightjar deploys to a single VPS with Docker Compose. [`DEPLOY.md`](DEPLOY.md) has the full walkthrough:
picking a provider, DNS, [`deploy/bootstrap.sh`](deploy/bootstrap.sh) for a hardened Ubuntu box,
`docker compose up -d --build`, and [`deploy/backup.sh`](deploy/backup.sh) for nightly dumps.

It is **not** configured for Vercel or similar serverless hosts. The hub is a long-running service with a
background uptime probe, which serverless platforms freeze between requests.

> **Do not deploy the current stack to the public internet as-is.** See the first two items below.

---

## Roadmap

| Chunk | What | State |
|---|---|---|
| 1 | Models, fingerprinting, ingest, HMAC, reporter, dedupe test | Done |
| 3 | React console, uptime probe, incident actions, tickets, deploys, idle screen | Done |
| 2 | Alert policy, presence, Pushover, speaker | **Next, blocked on cost.** The whole point of the project |
| 4 | Email intake and the 07:30 digest | Not started |
| 4b | Fleet onboarding: published reporters and an `add-site` CLI | Only worth doing at the third or fourth app |
| 5 | Wall display route and kiosk | Designed. The idle screen exists |
| 6 | Pico companion: status strip, fingerprint, watchdog | Designed |
| 7 | Gitea integration | Designed. The generic deploy webhook exists |
| 8 | Read-only client status page | Build when a client asks twice |

---

## Known gaps

Recorded here so nothing looks finished that is not.

1. **The write endpoints have no authentication.** Anyone who could reach the hub could mute every issue.
   Fine on localhost, unacceptable on the internet. Gate them (Caddy basic auth or a shared token) first.
2. **The Caddyfile would expose `/api/*` publicly.** Its catch-all proxies everything not under `/ingest`
   to the hub. Together with the point above, deploying it unchanged exposes the console API.
3. **The console is not in the deploy stack.** `docker-compose.yml` and the Caddyfile do not build or
   serve `console/`.
4. **`create_all` on startup, not migrations.** It does not alter existing tables, so adding a column to a
   running database needs a manual `ALTER TABLE`. Replace it with Alembic before there is data worth
   keeping.
5. **The uptime probe lives inside the hub.** It shares the hub's failure domain, so it is not the
   dead-man's switch. Real uptime alerting still wants the second probe on another network.
6. **Unlock on the idle screen is navigation, not security.** The fingerprint is simulated, and there is no
   login to protect.
7. **`INTEGRATE.md` describes endpoints that do not exist yet**, such as `/ingest/{key}/webhook` and
   `/ingest/gitea`.
8. **A malformed request body to `/ingest/{key}/event` returns 500** rather than 422.
9. **The cabinet drawings predate the OLED and M920q decisions.** See [the cabinet](#the-cabinet).

---

## Conventions

- Python 3.11+, async SQLAlchemy 2.0 with `Mapped[...]` annotations.
- **Reporting must never break the app it reports on.** Every outbound call in the reporter is wrapped and
  swallowed.
- Ingest bursts serialise with `SELECT ... FOR UPDATE` on the issue row. Intentional: correctness over
  throughput at this scale.
- `notify.py` (chunk 2) stays a single adapter exposing one `send(ticket, priority)` function. Nothing else
  may know which push provider is in use. That is the swap point if Pushover is ever replaced.
- Comments explain *why*, not *what*. The fingerprint exclusions and the serialisation choice are where
  future-you will want the reasoning.

---

## Out of scope

Named so they do not creep in: a client portal with logins, SLA timers, comment threads, multi-user auth,
replying to clients from inside the app, request-volume graphs, a custom SDK per language, Datadog, Jenkins.

Also out of scope: **rebuilding tools that exist.** Sentry does capture, Grafana does metrics. Nightjar's
job is narrow: deduplicate, decide, deliver.

---

## Documentation map

| File | What it holds |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | The software build, and the rules that govern it. Read this first |
| [`START-HERE.md`](START-HERE.md) | Where the project stands and what to do next |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Every decision, what was rejected, and why. Read before reversing anything |
| [`docs/FULL-DESIGN.md`](docs/FULL-DESIGN.md) | Hardware, access tiers, and the later chunks |
| [`DEPLOY.md`](DEPLOY.md) | Getting onto a VPS |
| [`INTEGRATE.md`](INTEGRATE.md) | Wiring in client apps, uptime monitors, Sentry and Gitea |
| [`mockups/`](mockups/) | Working HTML: console, wall display, boot splash, detail drawer |

---

## Brand

Ink `#0A0A0B` · bone `#F2EFE6` · amber `#E8A33D`

Amber is reserved for alert state: warning dots and P2 badges, the status light, the bird's eye in the logo.
If amber appears on ordinary UI it stops meaning anything. In the console, P1 is **red** and P2 is amber:
red means "wake me", amber means "look soon".

Assets: [`nightjar-wordmark.svg`](assets/nightjar-wordmark.svg), [`nightjar-icon.svg`](assets/nightjar-icon.svg)
and [`nightjar-mark.svg`](assets/nightjar-mark.svg) (monochrome, inherits `currentColor`).

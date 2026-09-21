# Nightjar

Incident hub for a solo developer who maintains client websites and systems
alongside a full-time job. Stack traces and client reports go in, a small
number of well-chosen phone notifications come out.

Named for a bird that only becomes active after dark — which is when this
thing will contact you.

---

## Why this exists

The operator has a day job. The scarce resource is not compute or storage, it
is **attention at inconvenient hours**. Every design decision in this repo is
downstream of that.

Two consequences worth internalising before changing anything:

1. **A notification that did not need to be sent is a bug**, of the same
   severity as a missed one. If the operator mutes the app, the system has
   failed completely.
2. **The dashboard is not the product.** The phone is. The web console exists
   for the morning triage pass and for answering "is my client's site okay?"
   — not for staring at.

---

## Architecture

```
App exception ─┐
Uptime check  ─┼─→ ingest ─→ fingerprint & group ─→ alert policy ─┬─→ Pushover (P1, retry until acked)
Client email  ─┘                                                  └─→ daily digest
```

Single FastAPI service, Postgres, small React console. Everything else is a
client of it.

### Deliberate boundaries

- **We do not build error capture.** Sentry or self-hosted GlitchTip can do
  the heavy lifting and forward via webhook. What we own is the deduplicated
  ticket store and the decision of whether to buzz the phone.
- **We do not build a native iOS app.** Pushover is the iOS app. A native
  build means a developer account, APNs certs, provisioning profiles and a
  Swift codebase, all to deliver a push notification. If an own-branded phone
  surface is ever wanted, the path is a PWA (iOS supports web push for
  home-screen apps from 16.4), not native.
- **Clients do not get a login.** They get an email address. A business owner
  will not learn a ticketing UI; they will email or WhatsApp. A read-only
  tokenised status page comes later, when a client asks for it twice.
- **The hub does not share a failure domain with what it monitors.** Deploy it
  on a separate VPS (Hetzner, DigitalOcean, Fly). Never on client
  infrastructure, never on the Raspberry Pi at home. Pair it with an external
  dead-man's-switch heartbeat so something tells you when the hub itself dies.

---

## Core domain concepts

### Fingerprint

The hash that turns 1,284 identical exceptions into one row. `hub/fingerprint.py`.

Composed of: `site_id` + exception type + **normalised** message + the
innermost `FRAME_DEPTH` frames as `module:function`.

Deliberately excluded:

- **Line numbers** — they shift whenever you edit a file above the bug. A
  fingerprint that changes on reformat is worthless.
- **Per-occurrence values** — IP addresses, UUIDs, emails, quoted strings and
  numbers are normalised to placeholders before hashing.

Both failure modes are real and opposite. Too loose and unrelated bugs merge,
so you only ever hear about one of them. Too tight and every occurrence
becomes its own row and the console is unreadable. Any change here needs the
tests in the fingerprint section below to still pass.

### state_change

The seam between ingest and alerting. `POST /ingest/{key}/event` returns one
of:

| Value        | Meaning                                             | Alerts? |
|--------------|-----------------------------------------------------|---------|
| `new`        | first time this fingerprint has been seen           | yes     |
| `regression` | was resolved or snoozed, and has come back          | yes     |
| `spike`      | crossed `SPIKE_THRESHOLD` inside `SPIKE_WINDOW_MIN` | yes     |
| `null`       | already known, already open, nothing has changed    | no      |

**Alert on state change, not on events.** This single rule is the difference
between a system that gets used and one that gets muted in a week.

### Issue status

`open` · `resolved` · `muted` · `snoozed`

`muted` matters more than it looks. Some errors cannot be fixed today — a
flaky third-party SDK, bots hitting a dead route. Muted issues keep counting
but never generate a `state_change`, including on regression. Without mute,
the noise poisons the list and the operator stops trusting it.

### Presence and output channels

Presence controls **which channels fire**, never **whether an incident alerts**.

> Being `home` may only ever *add* the speaker. It must never suppress a phone
> push. The failure mode otherwise: operator leaves, forgets to tap out, system
> believes he is home, plays a sound to an empty house and skips the push. The
> incident is then invisible.

Presence is stored with an expiry, not as a boolean toggle — a toggle desyncs
permanently the first time a tap is missed.

```python
class Presence(Base):
    state: Mapped[str]           # home | away
    source: Mapped[str]          # nfc | geofence | manual
    expires_at: Mapped[datetime]
```

- An NFC tap sets `home` with a TTL (default 840 minutes / 14 hours).
- On expiry, or with no record at all, the effective state is **`away`**.
- `away` is the safe default because it is the state where only the reliable
  channel (push) is used. Always fail toward the reliable channel.

Inputs, in order of trust: NFC tap (explicit), geofence automation (catches
forgotten taps, flaky and delayed — harmless given the TTL), manual override in
the console.

### Routing matrix

|                | P1                            | P2                | P3     |
|----------------|-------------------------------|-------------------|--------|
| Home, daytime  | push + speaker                | push + soft chime | digest |
| Home, night    | priority-2 push + speaker     | push only         | digest |
| Away           | priority-2 push               | push              | digest |

P2 is silent at night regardless of presence. Letting presence override quiet
hours reintroduces exactly the problem quiet hours exist to solve.

### Speaker

Audio out on the host, `aplay` invoked from the policy layer, distinct WAV per
severity, volume set per-severity via `amixer` so P1 is audibly louder than P2.

Two rate limits are required from day one, not later:

1. One sound per issue per 15 minutes.
2. A global cap of N sounds per hour.

Without both, a spike machine-guns the speaker and it gets unplugged inside a
week. Optional upgrade: local neural TTS via `piper` so the announcement names
the affected client — no cloud round-trip, and it removes the need to look at a
screen to know who is affected.



Derived from the source, not chosen by hand.

| Severity | Source                                        | Night behaviour                    |
|----------|-----------------------------------------------|------------------------------------|
| P1       | uptime failure, `fatal` level                 | wakes the phone, retries until ack |
| P2       | `error` level, rate spikes                    | normal push, quiet hours respected |
| P3       | client email, `warning` level                 | digest only                        |

---

## Repository layout

```
hub/
  config.py        pydantic-settings, reads .env
  db.py            async engine, session factory, declarative Base
  models.py        Client, Site, Issue, Event
  fingerprint.py   normalise + hash — the load-bearing file
  schemas.py       request/response models
  ingest.py        POST /ingest/{key}/event
  main.py          app factory, lifespan, healthz
client/
  reporter.py      the whole "SDK" — drop into any managed FastAPI app
  thermal_probe.py posts host thermals in as ordinary tickets
deploy/
  bootstrap.sh     fresh Ubuntu VPS -> docker, ufw, ssh hardening
  Caddyfile        TLS + reverse proxy
  backup.sh        nightly pg_dump, retention, optional rclone offsite
mockups/           working HTML of the console, wall, boot splash, drawer
assets/            logo + cabinet drawings
tools/             drawing generators
Dockerfile, docker-compose.yml
seed.py            creates one client + site, prints ingest keys
test_ingest.sh     fires 200 varied copies of one crash; expect ONE issue
CLAUDE.md          this file — the reasoning
DEPLOY.md          getting onto a VPS
INTEGRATE.md       wiring client apps, uptime, Sentry, Gitea in
```

`notify.py` (chunk 2) must stay a single adapter exposing one
`send(ticket, priority)` function. Nothing else in the codebase should know
which push provider is in use — that is the swap point if Pushover is ever
replaced with ntfy or web push. The serial adapter for the Pico (chunk 6)
follows the same rule, and must additionally no-op when no device is attached.

## Build order

**Chunk 1 — done.** Models, fingerprinting, ingest endpoint, HMAC
verification, the reporter snippet, the dedupe test harness.

**Chunk 2 — next, and the one that has to be right.** Alert policy, presence,
Pushover dispatch, speaker.

- Policy reads `state_change`, `severity` and effective presence, applies quiet
  hours (22:00–07:00 Africa/Nairobi, P1 only), and returns a decision naming
  the channels to use — see the routing matrix above.
- `POST https://api.pushover.net/1/messages.json` with `token`, `user`,
  `title`, `message`, `priority`, `url`, `url_title`.
- Priority 2 additionally needs `retry` (60), `expire` (1800), `sound`, and a
  `callback` URL. Pushover POSTs that callback when the notification is
  acknowledged in the app, so **no separate signed ack link is needed** —
  handle it at `POST /pushover/ack`.
- `POST /presence` accepts `{state, ttl_minutes, source, token}`. Called by iOS
  Shortcuts NFC automations: NTAG213 stickers, two tags (`arriving` inside the
  door frame, `leaving` by the keys) because one tag maps to one Shortcuts
  automation. Disable "Ask Before Running" on both.
- Speaker dispatch behind the same adapter boundary as push, with the rate
  limits above enforced in the policy layer, not in the player.
- Test by deliberately breaking a staging route, not with a unit test. Test the
  presence path by tapping out and confirming the push still arrives.

**Chunk 3.** React console — one page, ~300 lines, two endpoints
(`/api/sites/health`, `/api/issues`), 30-second polling, no websockets.
Site health strip on top, issue list below sorted by severity then recency,
drawer on click with the stack trace and resolve/mute/snooze. Ugly is fine.

**Chunk 4.** Email intake (Postmark or Mailgun inbound → `/ingest/email`) and
the 07:30 digest cron. Last because nobody's site is down when a client emails
about a typo.

**Chunk 5.** Wall display — `GET /wall`, the tier 1/2 screen. See
`mockups/nightjar-wall.html`. Auth is by *being on the machine*: the kiosk hits
`127.0.0.1` and Nightjar returns **404** (not 403 — do not confirm the route
exists) to any other address. Plus an enrollment cookie, set once from a
single-use code, stored hashed, revocable. **No token in the URL** — it lands in
history and access logs.

`cage` + `chromium --kiosk --user-data-dir=/var/lib/nightjar-wall` as a systemd
unit with `Restart=always`. Boot splash (`mockups/nightjar-boot.html`) covers
Chromium's startup flash; each line goes green as the hub confirms a dependency,
so a line that *doesn't* go green is a diagnostic before you open a terminal.

Cache the last good response — an ISP blip should show stale data, not an error.

**Chunk 6.** Pico companion. Scope reduced: the OLED is **dropped** in favour of
the wall monitor. Remaining jobs: WS2812 status strip, R307 fingerprint, fan
PWM, heartbeat watchdog.

Serial protocol over `/dev/ttyACM0`, one line per message (`STATE red pulse 60`).
The adapter **must be a no-op when nothing is attached** — log once, carry on.

The watchdog is deliberately dumb: Nightjar pulses every 10 s, the Pico resets a
timer, and after 60 s of silence it blinks slow white on its own. It knows
nothing about Nightjar or the sites. A smarter watchdog would need to know what
healthy looks like, and could therefore be wrong about it.

The RP2040 draws ~0.3 W. **It needs no fan.** Passive slots only. Mount its
enclosure to the cabinet or backplate — never drill the ThinkCentre chassis
(conductive swarf, and that case is the airflow duct).

**Chunk 7.** Gitea integration — `POST /ingest/gitea`, `deployments` table
(site, environment, sha, branch, status, actor, timestamp), deployments strip on
the console. Reuses the existing fingerprinting, so repeated failed deploys of
the same commit collapse into one issue. ~150 lines.

**Chunk 8.** Client status page — `status.yourdomain/c/<token>`, read-only, no
login. Their sites' uptime and their tickets with a plain status, plus a short
public note. Two fields per ticket: internal notes and client-visible note.

Never: a login (password resets become support requests about the support
system), a submission form (email already works), or live error counts ("your
site threw 1,284 exceptions" is true, alarming, and unactionable).

Build it when a client asks for it twice. ~200 lines over data you already have.

**Done:** `create_all` on startup was replaced with Alembic migrations (see the
README's "Tests and migrations").

---

## Deployment — where things run

Two machines, split by consequence. **Nightjar lives on the VPS, permanently.**
This is not a stopgap until hardware arrives.

| VPS (Ubuntu LTS, 2 vCPU / 4 GB) | ThinkCentre M920q (Ubuntu LTS, headless) |
|---|---|
| Nightjar hub | Staging environments, one stack per client |
| Postgres | Gitea (self-hosted Git + Actions) |
| Caddy — the only ingress | Grafana |
| External heartbeat ping | Uptime Kuma — **second** probe |
|  | Backup target, pulls nightly from the VPS |
|  | Personal planner, email tooling |
|  | Chromium kiosk driving the wall display |

The rule: **Nightjar must be more reliable than the things it watches.** A
watchdog at home inherits the house's ISP and power and maintenance mistakes,
and when it dies it goes silent — which reads identically to "all clear". The
Tiny gets every workload where two hours of downtime is merely annoying.

A consequence worth stating plainly: the wall display runs *on* the Tiny but
fetches from the VPS over HTTPS. Turning the screen off, or losing home
internet, does not affect alerting at all.

### Hardware

ThinkCentre **M920q**, i7-8700T (6c/12t, 35 W), 8 GB stock — add a 16 GB SODIMM.
Chosen over the HP ProDesk 600 G4 SFF: the SFF is ~8 L, uses a 65 W desktop CPU
plus a PSU fan, and idles far higher. The M920q also has the PCIe riser the
M720q lacks.

Official dimensions 37 × 179 × 183 mm. Lenovo's own convention assumes it
stands **vertically** — there is a vertical stand as a self-service CRU.

Not Windows Server: per-core licensing with a 16-core minimum means a 6-core
Tiny pays for 16 (~$1,176 Standard, plus CALs, free only as a 180-day eval).
Beyond cost, `uvloop` and `httptools` do not build on Windows, `gunicorn` does
not run there, and there is no systemd.

### Airflow — confirmed from chassis photos, not documentation

Lenovo's manual does **not** specify airflow direction anywhere. Read from the
hardware itself:

- **intake**: large hex mesh at the *bottom of the front bezel*
- **exhaust**: vertical slot column on the *rear face*, beside the I/O
- **exhaust**: slot band on the *top face* when upright

Standing vertically, its native path is front-bottom in, rear-and-top out — a
diagonal that works with convection.

**Consequence for the cabinet: a glass door cannot sit flush against the front
bezel.** That hex mesh is the intake. Needs a ~35 mm front plenum fed from
below, which is the whole reason for the split-front design (glass upper,
solid lower band carrying intake, ports and the decorative light switch).

Still to do: **tissue test each face under load before cutting steel.** Vents
read from a product photo are a strong hypothesis, not a measurement.

### Acceptance targets for any enclosure

| Measure | Target |
|---|---|
| Intake air vs room ambient | within 5 °C |
| CPU package, sustained load | under 80 °C |
| NVMe composite | under 65 °C |
| Throttle events over 30 min | zero |

`cat /sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count` gives
the last one directly. Take an open-air baseline *before* the cabinet exists —
it cannot be recovered afterwards. `client/thermal_probe.py` reports breaches
into Nightjar as ordinary tickets.

Power is covered by an inverter with ~2 days runtime, so power loss is not the
risk to design around. ISP outage and box failure are.

### The dead-man's switch is not optional

Both the Pushover call and the speaker are driven by the hub, so they share its
fate. If the hub dies it does not complain — it goes **silent**, and silence is
indistinguishable from everything being fine. That is the one failure this
system cannot have.

Nightjar pings an external heartbeat service (healthchecks.io, Cronitor) every
5 minutes. If the pings stop, *they* alert the phone. This is the only
mechanism that converts silence back into a signal, and it is the reason
self-hosting is defensible at all.

### Wall display unit

`cage` is a Wayland kiosk compositor: one fullscreen app, no desktop, no window
manager, no dialogs that can appear over the dashboard. Run it as a dedicated
unprivileged `wall` user with `Restart=always` — if Chromium dies at 2am the
wall must come back without intervention. Disable blanking via `consoleblank=0`
on the kernel command line.

---

## Out of scope

Named here so they do not creep in: client portal with logins, SLA timers,
comment threads, multi-user auth, replying to clients from inside the app,
request-volume graphs, uptime percentage charts, a custom SDK per language.

Each is a real feature. Each would prevent this shipping this month. If graphs
are ever genuinely wanted, point Grafana at the same Postgres rather than
building them here.

---

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # point DATABASE_URL at postgres
python seed.py                # prints ingest_key and ingest_secret
uvicorn hub.main:app --reload
./test_ingest.sh <ingest_key>
```

`test_ingest.sh` sends 200 copies of one crash with a different IP address and
retry count in each message. Correct output is a single `issue_id` with
`event_count` in the 200s. Two hundred separate issues means the fingerprint
is broken.

### Environment

| Variable           | Purpose                                            |
|--------------------|----------------------------------------------------|
| `DATABASE_URL`     | `postgresql+asyncpg://...`                          |
| `VERIFY_SIGNATURES`| HMAC check on ingest. Off in dev, **on in prod**    |
| `SPIKE_THRESHOLD`  | events inside the window that constitute a spike    |
| `SPIKE_WINDOW_MIN` | the window, in minutes                              |

---

## Observability stack

**Sentry yes, Datadog no.**

Sentry (or self-hosted GlitchTip, SDK-compatible) does exception capture
properly: source context, local variables, release tagging, breadcrumbs,
frontend source maps. Free tier covers a handful of client sites. It feeds
Nightjar by webhook. Sentry owns *capture*; Nightjar owns *"is this worth your
attention at 2am"*.

Datadog is rejected on architecture, not only price. It already does alerting —
monitors, routing, escalation, quiet hours. Paying for it means maintaining two
systems that decide when to interrupt you, which is precisely the fragmentation
Nightjar exists to remove. (Cost is also real: ~$15/host/month infrastructure,
~$31/host APM, billed on a 99th-percentile high-water mark.)

Grafana reads the same Postgres for metrics. **Do not rebuild Grafana inside
Nightjar** — inline sparklines in the issue drawer show shape around the moment
of failure; "Open in Grafana" deep-links to the real dashboard for analysis.

If a client ever mandates Datadog, run it for that client's infrastructure and
webhook it in like any other source. Never as the default.

## External services

| Service                | Role                                | Notes                                                                 |
|------------------------|-------------------------------------|-----------------------------------------------------------------------|
| Pushover               | iOS delivery                        | ~$4.99 one-time per platform after a 30-day trial. Priority 2 retries until acknowledged |
| Cloudflare Email Routing | forwards `support@` into the hub  | free; forwards to a human mailbox and the inbound webhook at once      |
| Postmark / Mailgun     | inbound parsing, transactional send | separate subdomain for sending                                        |
| Uptime Kuma            | uptime checks                       | catches the case where the app is too dead to report its own errors    |
| Sentry / GlitchTip     | optional error capture              | forwards to `/ingest/{key}/webhook`                                    |

**Send automated mail from a subdomain** (`mail.nightjar.example`) with its own
SPF and DKIM. If a bug ever loops and fires 400 confirmations, the reputation
damage lands there and not on the domain clients receive invoices from. Start
DMARC at `p=none`, tighten once the reports are clean.

**iPhone setup gotchas:** add Pushover to the Focus mode allowlist or P1
retries wait politely until morning, and give it a distinct sound so a site
outage does not sound like a WhatsApp message.

---

## Access tiers

Three levels, each with a different threat model. Two of these are conveniences
and only one is authentication.

| Tier | Surface | Gate | Shows |
|---|---|---|---|
| 1 Ambient | Wall monitor | none | Verdict line, site names, 90-day uptime ribbons, percentages |
| 2 Limited | Wall monitor | R307 fingerprint, **2 min** | Issue titles, counts, ages. Nothing diagnostic |
| 3 Full | Console | Passkey (WebAuthn) | Stack traces, local variables, metrics, resolve/mute/snooze |

**Tier 2 exists to answer "do I need to get my laptop?" and nothing more.**
The R307 matches on-module and returns only a template ID over serial — the
fingerprint itself never travels or is stored anywhere. It is convenience-grade
(a lifted latent defeats an optical sensor), which is fine for unblurring a
screen in your own house and unacceptable for anything that changes state.

Tier 3 uses passkeys, which *are* cryptographic — private key in the phone's
secure element, phishing-resistant. Keep a password fallback for a lost phone.

The wall reveal is 2 minutes, shorter than the console's 5, because it is an
unattended screen in a room people walk through. Reversion must be automatic,
never a button — you will walk away without pressing it.

**What gets redacted is detail, not names.** A site name tells a guest little;
`postgresql://app@10.0.0.14:5432/sacco` in frame three of a stack trace tells
them a great deal. Site names, severity, uptime and deploy status stay visible
at tier 1.

### The invariant across all of it

Presence, fingerprint, passkey, wall, speaker, status light — **none may ever
suppress an alert.** Each may add a channel or reveal detail. If every one of
them fails simultaneously, Pushover still fires. The phone is load-bearing;
everything else is decoration on top.

## Console design

See `mockups/` for working HTML: `nightjar-console.html` (boot splash →
dashboard → detail drawer), `nightjar-wall.html` (tiers 1 and 2).

**Stack:** React 19 + Vite, served by FastAPI, behind Caddy (not nginx — Caddy
handles certificates automatically). Recharts for inline sparklines.

Layout, top to bottom:

1. **Verdict line** — the one loud element. "All systems operational" in green,
   or "One site is down" in red. 95% of the time this is the only thing that
   needs reading from across a room. Everything else is quiet by comparison.
2. **Site cards** with 90-day uptime ribbons, one segment per day coloured by
   that day's worst state. A ribbon beats a percentage: 99.7% from one six-hour
   outage and 99.7% from forty short blips are the same number and completely
   different problems. Grey segments mark days before monitoring began, so "no
   data" never reads as "all good".
3. **Deployments** — read-only, from Gitea. Site, environment, ref, status.
4. **Client requests** — change-overs, incident reports, access requests. Kept
   separate from crashes because the clock is different: a crash is measured
   against error rate, a client request against a promise made to a person.
   "3d overdue" is a different kind of pressure than "×1,284".
5. **Open issues** — severity, site, detail, count, age.

The **detail drawer** carries three things together, because when something
breaks you want them in one place: inline metrics around the failure, the
exception with local variables, and the **incident log**.

The incident log is not application logs — it is *what Nightjar did*. Alert
sent, speaker suppressed because presence was away, spike detected but no
second alert, retry 3 of 30, last deploy correlated. This is how you debug the
alerting itself when you ask "why didn't my phone buzz?", and where you notice
your thresholds are wrong.

Correlating against the deployments table ("last deploy 2h ago, not a recent
change") is the highest-value line in the drawer and nearly free once Gitea
webhooks exist.

## Gitea

Self-hosted on the Tiny. Same Git commands, different remote. Devs never get
SSH to any server — Gitea is the control point, and revoking someone is one
click rather than hunting through `authorized_keys`.

**Branch protection on `main`:** empty Push Whitelist (nobody pushes directly,
including you), Merge Whitelist containing only you, require 1 approval,
restrict approvals to whitelisted users, dismiss stale approvals on new
commits, require status checks. Gitea Actions uses GitHub Actions syntax and
deploys to staging on push to `develop`.

Webhook payloads carry `pusher` and per-commit `author`, which can differ.
Store both — at 2am, "who put this on the server" matters more than "who wrote
it".

| Gitea event | Becomes |
|---|---|
| `push` to `main` | P2 — protection bypassed or rules changed |
| `pull_request` opened | P3, names the author |
| PR open > 48h | escalates to P2 |
| `deployment_status` failure | P1 production, P2 staging |

Keep that first one even with protection enabled: repo admins can edit branch
rules, so a direct push to `main` should always surface.

## Conventions

- Python 3.11+, async SQLAlchemy 2.0 with `Mapped[...]` annotations.
- Reporting must never break the app it reports on — every outbound call in
  `client/reporter.py` is wrapped and swallowed.
- Ingest bursts are serialised with `SELECT ... FOR UPDATE` on the issue row.
  This is intentional: correctness over throughput at this scale.
- Comments explain *why*, not *what*. The fingerprint exclusions and the
  serialisation choice are the two places future-you will want the reasoning.

---

## Brand

Ink `#0F1420` · bone `#F2EFE6` · amber `#E8A33D`

Amber is reserved for alert state — the bird's eye in the logo, P1 badges in
the console, nothing else. If amber starts appearing on ordinary UI, it stops
meaning anything.

Assets in `assets/`: `nightjar-icon.svg` (app icon, favicon),
`nightjar-mark.svg` (monochrome, inherits `currentColor`),
`nightjar-wordmark.svg` (convert the text to outlines before shipping), plus
cabinet drawings (`case-*.svg`, `front-panel-detail.svg`, `mount-layout.svg`)
and their generator in `tools/`.

The boot splash uses original geometric glyphs, not imitations of project
logos. Replace them with each project's official SVG from its brand page — one
line each in the `GLYPHS` object — and check their brand guidelines while you
are there.

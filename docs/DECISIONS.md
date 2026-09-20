# Decision log

Every significant choice, what was rejected, and why. Written so that a future
reader — or a future you — does not relitigate settled questions, and knows
which decisions were made on evidence versus on judgement.

Ordered roughly as they were made.

---

## 1. The problem being solved

Solo developer maintaining client websites alongside a full-time job. The
scarce resource is **attention at inconvenient hours**, not compute or storage.

Everything downstream follows from that. A notification that did not need to be
sent is a bug as serious as a missed one, because the end state of over-alerting
is a muted app — which is indistinguishable from having built nothing.

---

## 2. Build the hub, not the capture layer

**Decision:** Nightjar owns deduplication, routing and delivery. It does not own
exception capture.

**Rejected:** building an SDK per language, and building source-context capture.

Sentry and GlitchTip already do capture properly — source context, local
variables, release tagging, breadcrumbs, frontend source maps. Reimplementing
that is months of work for a worse result. They webhook in.

The narrow job that is genuinely unsolved for this use case is *"is this worth
interrupting Brandon at 2am."*

---

## 3. Alert on state change, not on events

**Decision:** a push fires only on `new`, `regression`, or `spike`. Repeat
occurrences of a known open issue are silent.

This is the single most important rule in the system. Without it, one bad deploy
produces 4,000 notifications and the app gets muted within a week.

The `state_change` field returned by the ingest endpoint is the seam this is
built on.

---

## 4. Fingerprinting excludes line numbers

**Decision:** hash `site_id` + exception type + normalised message + innermost 3
frames as `module:function`. Exclude line numbers. Normalise IPs, UUIDs, emails,
quoted strings and numbers to placeholders.

**Reasoning:** line numbers shift whenever you edit anything above the bug. A
fingerprint that changes on reformat is worthless.

Both failure directions are real and opposite: too loose merges unrelated bugs
so you only hear about one; too tight makes every occurrence its own row and the
console unreadable.

**Verified:** same bug with different IPs and retry counts collapses; shifted
line numbers collapse; different exception types and different sites stay
separate.

---

## 5. `mute` is a first-class status

**Decision:** four statuses — `open`, `resolved`, `muted`, `snoozed`. Muted
issues keep counting but never produce a `state_change`, including on
regression.

Some errors cannot be fixed today: a flaky third-party SDK, bots hitting a dead
route. Without mute, that noise poisons the list and the operator stops trusting
it — the same end state as over-alerting.

---

## 6. Pushover, not a native iOS app

**Decision:** Pushover for delivery. ~$5 one-time per platform.

**Rejected:** a native iOS app. That means a $99/year developer account, APNs
certificates, provisioning profiles, TestFlight, and a Swift codebase to
maintain forever — all to deliver a push notification. Sideloaded builds expire
every 7 days without a paid account.

**Middle path noted for later:** a PWA. iOS supports web push for home-screen
apps from 16.4, which could eventually replace Pushover. Month six, if ever.

Priority 2 is the mechanism that matters: retries every 60s for 30 minutes until
acknowledged. Its `callback` URL means **no separate signed ack link is needed**
— an earlier design that was dropped.

---

## 7. Clients get an email address, not a portal

**Decision:** `support@` → Cloudflare Email Routing → inbound webhook → ticket.
No login, no submission form.

**Reasoning:** a business owner will not learn a ticketing UI. They will email,
or WhatsApp. A portal nobody logs into is wasted work, and a login means
password resets, which means support requests about the support system.

**Deferred, not rejected:** a read-only tokenised status page
(`status.yourdomain/c/<token>`). The complaint you will actually get is not "I
wish I could file tickets" but *"any update on that thing?"*, repeatedly, by
WhatsApp. Build it when a client asks twice. Chunk 8.

---

## 8. Nothing may suppress an alert

**Decision, applied to every subsequent feature:** presence, fingerprint,
passkey, wall display, speaker, status light — each may *add* a channel or
reveal detail. None may be the reason a push does not fire.

This is why presence is stored with an expiry rather than as a boolean toggle.
A toggle desyncs permanently the first time a tap is missed; a TTL lapses on its
own, and the default with no record is `away` — the state where only the
reliable channel is used.

**Always fail toward the reliable channel.**

---

## 9. Linux, and where things run

**Decision:** Ubuntu Server LTS everywhere. Nightjar on a VPS, permanently.

**Rejected — Windows Server:** licensed per physical core with a 16-core
minimum, so a 6-core machine pays for 16 (~$1,176 Standard, plus CALs, free only
as a 180-day evaluation). Beyond cost, `uvloop` and `httptools` do not build on
Windows, `gunicorn` does not run there at all, and there is no systemd.

**Rejected — hosting Nightjar at home:** a watchdog must not share a failure
domain with what it watches. When it dies it goes *silent*, and silence reads
identically to "all clear". That is the one failure this system cannot have.

The inverter gives ~2 days runtime, so power was removed from the risk list —
but ISP outage and box failure remain, and both are enough.

---

## 10. The dead-man's switch

**Decision:** an external heartbeat service (healthchecks.io or similar) that
alerts when Nightjar *stops* pinging.

Raised three separate times during design, because it is the thing that converts
silence back into a signal. Without it, self-hosting is not defensible at all.

The local version — Nightjar pulses the Pico every 10s, which blinks slow white
after 60s of silence — covers the case where you are standing in the room and
the wall monitor is showing a frozen page that looks perfectly healthy.

The Pico watchdog is **deliberately dumb**: it counts, it does not check. A
smarter watchdog would need to know what healthy looks like, and could therefore
be wrong about it.

---

## 11. Hardware: the Tiny is secondary

**Decision:** ThinkCentre M920q, i7-8700T, upgraded RAM. Staging, Gitea, builds,
backups, a second uptime probe, and the wall display. **Not** Nightjar.

**Rejected — HP ProDesk 600 G4 SFF:** ~8 L, 65 W desktop CPU plus a PSU fan,
idles far higher. The M920q is 1 L at 35 W and has the PCIe riser the M720q
lacks.

The second uptime probe is the non-obvious value: two probes on different
networks let Nightjar distinguish "the client's site is down" from "my home
internet is down". One probe cannot, and you will otherwise be woken by your
own ISP.

**Note:** the project started with the hardware and moved to VPS-first. That was
the right correction — nothing in chunks 1–4 touches hardware.

---

## 12. Airflow was corrected by evidence, not assumption

Initial guidance about the chassis vents was **overconfident and partly wrong**.
Lenovo's maintenance manual does not document airflow direction anywhere — front
and rear views label only ports and indicators.

Corrected from a product photo:

- **intake**: hex mesh at the bottom of the front bezel
- **exhaust**: vertical slot column on the rear, beside the I/O
- **exhaust**: slot band on the top face when upright

**Consequence:** a glass door cannot sit flush against the front bezel. Needs a
~35 mm front plenum fed from below — which is why the front is split, glass
upper and solid lower band.

**Still unverified:** the tissue test under load. A product photo is a strong
hypothesis, not a measurement. Do it before cutting steel.

The same principle applies to thermals generally: take an open-air baseline
*before* the cabinet exists, because it cannot be recovered afterwards.

---

## 13. Redact detail, not names

**Changed mid-design.** The first wall mockup blurred client names. Revised to
blur *details* instead — stack traces, local variables, commit SHAs — while site
names, severity, uptime and deploy status stay visible.

**Trade-off acknowledged:** this is weaker privacy. A guest can see that
sacco-portal is down. But the thing that would actually embarrass you in front
of a client is `postgresql://app@10.0.0.14:5432/sacco` sitting in frame three of
a stack trace, not a red dot.

It also makes the screen dramatically more useful, which is the point of having
it.

---

## 14. Three access tiers, only one of them authentication

| Tier | Surface | Gate | Shows |
|---|---|---|---|
| 1 Ambient | Wall | none | Verdict, names, uptime ribbons |
| 2 Limited | Wall | R307 fingerprint, 2 min | Titles, counts, ages |
| 3 Full | Console | Passkey (WebAuthn) | Everything |

The R307 matches on-module and returns only a template ID — the fingerprint
itself never travels or is stored. It is convenience-grade; a lifted latent
defeats an optical sensor. Fine for unblurring a screen in your own house,
unacceptable for anything that changes state.

Tier 3 uses passkeys, which *are* cryptographic. Keep a password fallback for a
lost phone.

**Corrected during design:** an early sketch put the wall token in the URL
(`/wall?t=...`). Dropped — it lands in browser history and server access logs.
Replaced with loopback-only (404, not 403, to anything else) plus an enrollment
cookie stored hashed.

---

## 15. Uptime display: 90-day ribbons, not sparkbars

**Changed.** The original 24-bar sparkline looked like data and carried none.

A 90-day ribbon — one segment per day, coloured by that day's worst state —
shows what a percentage hides. 99.7% from one six-hour outage and 99.7% from
forty short blips are the same number and completely different problems.

Grey segments mark days before monitoring began, so "no data" never reads as
"all good".

---

## 16. Client requests are a separate section

**Decision:** change-overs, incident reports and access requests get their own
list, distinct from crashes.

The clock is different. A crash is measured against error rate; a client request
is measured against a promise made to a person. *"3d overdue"* is a different
kind of pressure than *"×1,284"*.

---

## 17. The incident log records what Nightjar did

**Decision:** the issue drawer carries a timeline of *the system's own actions* —
alert sent, speaker suppressed because presence was away, spike detected but no
second alert, retry 3 of 30, last deploy correlated.

Not application logs. This is how you debug the alerting itself when you ask
"why didn't my phone buzz?", and where you notice your thresholds are wrong.

Correlating against the deployments table is the highest-value line in the
drawer and nearly free once CI webhooks exist.

---

## 18. Rejected tooling

**Datadog** — rejected on architecture, not only price. It already does
alerting: monitors, routing, escalation, quiet hours. Paying for it means
maintaining two systems that decide when to interrupt you, which is precisely
the fragmentation Nightjar exists to remove. Cost is also real (~$15/host/month
infrastructure, ~$31/host APM, billed on a 99th-percentile high-water mark).

If a client ever mandates it, run it for that client's infrastructure and
webhook it in like any other source.

**Jenkins** — rejected. A JVM app wanting 1–2 GB idle, a separate auth system, a
separate URL, and a plugin ecosystem needing regular security attention. Gitea
Actions runs in the same stack with the same accounts and uses GitHub Actions
syntax.

**Important qualifier:** existing pipelines should *not* be migrated. A working
pipeline is worth more than consistency. CI is a client of the infrastructure,
not a component of it. The only thing to make uniform is that every pipeline
webhooks failures into Nightjar.

**Running a mail server** — rejected. Port 25 is blocked on most Kenyan
residential connections and by default on new VPS accounts. A fresh IP has no
sending reputation; Gmail and Outlook will bin mail for months while you fight
SPF, DKIM, DMARC and PTR. Use a real mailbox provider and build tooling on top.

---

## 19. Scope reductions made during design

- **OLED on the cabinet: dropped.** The wall monitor does the job. This removed
  the acrylic sub-panel, its cutout, the page layout, burn-in logic, and half
  the Pico's work.
- **Fan for the Pico: dropped.** The RP2040 draws ~0.3 W — less than a phone
  charger. Nothing to cool. Adding one buys noise, dust ingress, and a moving
  part that can seize.
- **Drilling the ThinkCentre: rejected.** Conductive swarf near a live board,
  and that chassis is the airflow duct. Mount to a backplate or the cabinet
  instead; the Tiny stays untouched and resellable.
- **Native app, client portal, tab-rotating dashboard, Grafana-inside-Nightjar:**
  all rejected as rebuilding things that exist or that nobody asked for.

---

## 20. Distribute the reporter as a package, but not yet

**Decision:** publish `@brandon/nightjar` (npm) and `nightjar-reporter` (PyPI)
once there are three or four apps reporting in. Not before.

Below that threshold, copying `client/reporter.py` into each project is less
work than maintaining a published package with versioning and a release step.
Above it, a bug fix means editing N copies, which is where the cost flips.

**The more important observation:** the reporter code is not the bottleneck at
scale. N apps means N `ingest_key`/`ingest_secret` pairs, N sets of environment
variables, and N deploys to remember. A package does nothing for that.

What does: an onboarding CLI (`nightjar add-site <name>`) that creates the site
and writes its credentials in one step, and keeping secrets in Gitea Actions
rather than in N `.env` files spread across N servers — so rotation is one edit
rather than a hunt.

**Constraint carried into the package unchanged:** it must never break the app
it reports on. Wrapped and swallowed, short timeout, no retry pile-up, no
unbounded background queue. Packaging makes that rule apply everywhere at once,
which is the actual argument for doing it.

---

## 21. Naming

**Nightjar** — a bird active only after dark, which is when the system will
contact you.

**Rejected for collisions:** Klaxon (Kotlin JSON library), Tripwire (security
product), Vigil (status-page tool), Watchtower (Docker utility), Kestrel
(ASP.NET web server), Beacon and Pulse (unsearchable).

**Rejected on principle:** anything describing the mechanism — TicketDesk,
TraceHub, ErrorBox. A name describing the v1 scope ages badly; this will grow
into uptime, status pages, possibly billing.

Swahili options (Kengele, Arifa, Zamu) were explored and set aside by preference.

---

## The current state, stated plainly

**Chunk 1 is done and verified.** A stack trace goes in, one deduplicated issue
comes out.

**Chunk 2 is not written.** Nightjar therefore cannot alert anyone. Everything
designed after it — the console, the wall display, the cabinet, the Pico, the
CI integration — sits on top of a system that currently dedupes crashes and
tells nobody.

The hardware is the fun part and the alerting is the entire point. That is a
known failure mode for this kind of project, and it is the reason this log ends
here rather than with more design.

**Next: the Pushover curl test, ten minutes. Then chunk 2, one evening.**

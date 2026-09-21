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

---

# Addendum — 2026-09-21

Written after the log above. Where this contradicts "Next: the Pushover curl
test", this wins.

## 22. Chunk 3 before chunk 2, because of money

**Decision:** build the console first and defer alerting.

Pushover is a paid licence, and production needs a domain and a VPS; none of
those were affordable. The console needs only Docker and Node, so it could be
built and verified locally at zero cost.

**Cost of the change:** the ordering argument above still stands — the console is
decoration on a system that does not alert. Nightjar cannot interrupt anyone
until chunk 2 exists, so it must not be trusted to catch a client outage.
Resume chunk 2 when the licence, domain and VPS are paid for.

---

## 23. Site health is derived from open issues, not stored

**Decision:** `down` = an open P1, `warn` = an open P2, otherwise `up`. Computed
per request from `issues`; there is no health table.

**Rejected:** faking the 90-day uptime ribbons (#15) or an "unacked" state. There
is no uptime probe and no acknowledgement yet, so any such number would be
invented. They arrive with uptime checks and chunk 2.

A derived state cannot drift out of sync with the issues it summarises.

---

## 24. The console never says "All quiet" while it cannot reach the hub

**Decision:** on a failed poll, the verdict becomes "Can't reach the hub" (red).
Last good data stays on screen, labelled with its age, rather than blanking.

This is #10 applied to the UI: a frozen or blind display that still looks
healthy is the one failure this system cannot have.

---

## 25. P1 is red and P2 is amber in the console

**Decision:** follow the mockup. This deliberately departs from the earlier brand
note that amber marks P1.

Red means "wake me", amber means "look soon". Both amber would blur the one
distinction that matters at 2am. Amber stays reserved for alert state.

---

## 26. Separate dev compose file, on port 5433

**Decision:** `docker-compose.dev.yml` publishes Postgres on `127.0.0.1:5433`.
`docker-compose.yml` stays the prod file and still never publishes the database.

**Why separate:** an override file would silently publish 5432 on the VPS. A
standalone file cannot leak into prod.

**Why 5433:** a native Postgres already held 5432 on the dev machine. Found by
running it, not by assumption — the first `up` failed to bind.

---

## 27. Neutral near-black, not navy

**Decision:** the console's ink is `#0A0A0B` with grey surfaces, replacing the
mockups' blue-tinted `#0F1420` family. Amber, red and green are unchanged.

Changed on the operator's preference after seeing it running. The brand section
in `CLAUDE.md` is updated to match; the mockups are left as the original design
reference. Because the palette lives in CSS custom properties, this was a token
swap, not a restyle.

---

## 28. The console grew past chunk 3's "two endpoints"

**Decision:** five tabs (Overview, Incidents, Requests, Deploys, Staging), each
with a tile on the homepage, backed by real tables.

Asked for by the operator after seeing the first version, which was a flat
list. The original "one page, ~300 lines, two endpoints" was a floor for the
first cut, not a ceiling. Tickets and deploys were chunk 4 and 5+ material and
were pulled forward because the data can be fed without paid services (a form,
and a CI `curl`).

**Rejected:** hard-coded sample rows in the UI. They would show fake numbers as
real, against the rule that a health display must never be invented.

---

## 29. Uptime probe lives inside the hub

**Decision:** a background task in the hub requests each `Site.url` every 60
seconds and stores results in `checks`.

**Known weakness:** the probe shares a failure domain with the hub, which #9
warns against for the watchdog itself. It is acceptable for the ribbon and for
console visuals; it is **not** the dead-man's switch (#10). Real uptime alerting
still wants a second probe on a different network (#11) and the external
heartbeat.

**Rules carried in:** two failed checks in a row before `down` (one is a blip);
a probe that has stopped is `warn`, never `up`; days with no checks are `gap`,
never green; errors store the exception class only, since messages can carry
URLs.

---

## 30. Incident actions are audited, and the row is locked

**Decision:** ack, resolve, mute, snooze and reopen each write an
`issue_actions` row and take the same `FOR UPDATE` lock as ingest.

The audit trail is #17's point: answer "why is this quiet?" weeks later. The
lock stops an action interleaving with an arriving event and leaving status and
counts disagreeing. Acknowledgement is derived from the trail and ends at the
next resolve or reopen, so a regression is not born already-acknowledged.

**Not yet true:** the log records human actions only. "Alert sent" and "speaker
suppressed" lines arrive with chunk 2.

---

## 31. Deploys arrive by webhook; staging is just a site

**Decision:** CI calls `POST /ingest/{key}/deploy` with `{ref, status}`,
authenticated like crash events. A staging environment is a site with
`env=staging`, so it reuses the probe, the ring and the ribbon with no new code.

Each status report is its own row (running, then success or failed). The console
hides a `running` row once a newer row exists for that site and ref, but never
hides a failure: a retry must not erase the failure it retried.

**Tension noted:** `CLAUDE.md` lists "uptime percentage charts" as out of scope.
The 90-day ribbon (#15) is the designed version of uptime display; the 24-hour
gauge ring is a small addition beyond it. Drop the ring if it starts to feel
like the charts that line was written to exclude.

---

## 32. The write endpoints have no authentication (resolved: see #42)

**Decision:** deferred, and recorded so it is not forgotten.

`/api/tickets` and `/api/issues/{id}/actions` accept unauthenticated writes.
That is fine on localhost and unacceptable on the internet: anyone could mute
every issue. Gate them (Caddy basic auth, or a shared token) before the hub is
deployed. Multi-user auth remains out of scope; a single shared secret is not.

---

## 33. The idle screen reverses #19, on purpose

**Decision:** add an idle screen that pages through production sites two at a
time, every 2 minutes.

#19 rejected a "tab-rotating dashboard" as something nobody asked for. This is
asked for, so it stands, but it is scoped to be less of the thing #19 feared: it
rotates *data pages* (two sites at a time), only on the idle screen, and never
rotates the working console. With two or fewer sites nothing moves.

It is also the first piece of the chunk 5 wall display, built in the console so
the same components serve both. `/wall` with loopback-only access (#14) is still
to come.

---

## 34. Reveal by fingerprint is simulated, and Unlock is not a lock

**Decision:** the idle screen hides incident titles and commit refs (detail, per
#13) until a reveal, which lasts 2 minutes and reverts on its own (#14). Until the
R307 reader exists, a labelled "Simulate fingerprint" button triggers it.
`useReveal()` in `hooks.js` is the single seam: the hub will report a match there.

**What Unlock is:** it navigates to Overview. #14 says a fingerprint is
convenience-grade and unfit for anything that changes state, and reserves the
console for a passkey. Until the write endpoints are gated (#32), the console has
no login when this was written (there is one now, see #42) and Unlock still only
navigates, so it protects nothing. It exists to give the flow its shape,
not to be a security boundary. Do not treat it as one.

**Rule kept:** the idle screen cannot suppress an alert. It only displays.
Timings (`?idle=&rotate=&reveal=`) are overridable in the dev server only, so a
production URL cannot lengthen how long detail stays revealed.

---

## 35. Staging stacks are pushed in, and shown read-only

**Decision:** a reporter on the staging host posts each Docker Compose project's
state to the hub (`POST /ingest/{key}/stack`). The Staging tab only displays it.

The hub is on the VPS (#9) and staging is on the ThinkCentre at home, so the hub
cannot see that machine's Docker, and must never be handed it: a socket exposed
to an internet-facing service is root on the box. Pushing in inverts the
direction, so nothing on the internet can reach a container. It reuses the
pattern `thermal_probe.py` already set.

**Rules carried in:** a reporter that has gone quiet is `reporter silent` and is
left out of the totals, never shown as healthy (#10). It only reports projects
named in its config, so unrelated containers never reach the console. It swallows
every failure and exits 0, like the app reporter. A running stack with no memory
limit is flagged, because one runaway app can take the whole machine.

**Found by testing:** keyed on site alone, two projects sharing one site key
overwrote each other. The feed is keyed on site and stack.

---

## 36. Control lives in Dockge over Tailscale, not in Nightjar

**Decision:** start, stop, restart and logs are done in Dockge on the staging
host, reached over Tailscale. The Staging tab links to it
(`VITE_STACK_CONTROL_URL`) and says it is read-only when that is unset.

**Rejected:** building start and stop into Nightjar. It would need write access to
Docker from something internet-facing, and it rebuilds a tool that exists (the
standing "do not rebuild tools that exist" rule). Portainer is an acceptable
alternative and Coolify or Dokploy go further, but Dockge fits the
one-directory-per-client layout with the least machinery.

**Costs, stated plainly:** the Docker socket is root-equivalent for both Dockge
and the reporter. Docker publishes ports straight through `ufw`, so the Dockge
compose file binds to the Tailscale address only and refuses to start without one.

---

## 37. Promotion readiness is derived, and pairing is by name

**Decision:** the Staging tab says whether a staging site can go to production,
computed from data already collected: deploys, uptime, open incidents and stack
state. Any blocker wins. A staging site is paired with its production site by
name (`<production>-staging`).

**Rejected:** a column linking the two sites. It is heavier, and a naming
convention is enough at this scale. It is a convention someone can get wrong, so
an unpaired site still gets every check and is only unable to be *compared*.

**Rules carried in:** a silent reporter blocks promotion (silence is not health,
#10); "in sync" never overrides a blocker, so a broken staging is never reported
as nothing to promote; uptime is floored, so 98.96% does not read as 99.

**Not a gate:** it advises, it does not stop anyone deploying. Nothing here can
block a deploy or suppress an alert.

---

## 38. Stack problems become issues, but only on the edge

**Decision:** a stack turning degraded, a restart loop, memory near its limit, or
low host disk opens an issue through the normal pipeline.

**Edge-triggered, deliberately.** A stack that stays degraded for an hour would
otherwise open sixty events, and 1,440 a day would trip the spike detector. Only
a problem that *just started* opens an event. This is #3 applied to the hub's own
checks: alert on state change, not on events.

To share the pipeline, the body of `ingest_event` was extracted unchanged into
`record_event`. Verified afterwards: the same fingerprint on the 200-crash harness,
and resolve-then-crash still returns a regression.

**Severity:** degraded and restart loops are P2. Memory is a warning, so P3 and
digest-only, and can never wake anyone.

**Not done at the time (built since, see #43):** issues are not auto-resolved on
recovery. That is a behaviour change worth deciding on its own.

---

## 39. Decommissioned stacks are removed by hand

**Decision:** a stack that stops reporting stays on the page as `reporter silent`
and blocks promotion until someone presses Remove. The hub refuses unless it is
really silent.

**Rejected:** hiding old stacks automatically, or pruning them faster. A stack
that has died looks exactly like one that was retired, and quietly making it
vanish is the failure this project is built to avoid (#10). An explicit act costs
one click and cannot hide a real outage.

---

## 40. A stop is not a crash

**Decision:** exit codes 0, 137, 143 and 255 mean a container was *stopped*. Any
other non-zero code is a crash, unhealthy for 15 minutes and simply down after
that, and an out-of-memory kill always counts.

Found with real data, not by guessing: a container killed a month earlier and a
stack exited 109 days earlier both read "degraded". Docker Desktop stopping its
VM, a reboot and `compose stop` all produce 137, 143 or 255, so "non-zero means
broken" raised a false alarm on every restart, which teaches you to ignore the
page.

**The blind spot, stated:** a crash from before the reporter started, or one that
stayed down past 15 minutes, reads as stopped. The crash was reported as an issue
when it happened if the reporter was watching, and the service carries a note
("crashed with exit code 1, 3 days ago") either way, so it is visible and not lost.

---

## 41. Migrations replace create_all, and adoption is checked

**Decision:** the schema is managed by Alembic and migrates on every start.
`create_all` never altered a table, so each new column needed a hand-run `ALTER
TABLE` and a database could drift from the code unnoticed. This was the
"before production" item, and it was done before there was data worth losing.

**Adoption is verified, not assumed.** A database made by the old `create_all` is
stamped as the baseline, upgraded, and then *checked* against the models. If it has
drifted, the hub refuses to start and undoes the stamp, because a stamp on a wrong
schema would make the next start believe everything is fine. `alembic check` is
the guard against a model changed without a migration.

**Tested against real databases,** including the refusal case. The dev database was
adopted with its data intact, and the production image migrated a fresh database
from inside the container.

---

## 42. One shared secret guards the console, and production refuses to run without it

**Decision:** `CONSOLE_TOKEN` guards everything under `/api`. Multi-user auth stays
out of scope; a single shared secret is not (#32). Ingest keeps its per-site key
and HMAC, and `/healthz` is open.

- **A session cookie, not a token in the page.** HttpOnly, so page JavaScript never
  holds the secret, and SameSite=Strict, so another site cannot make the browser
  send it. Scripts use a bearer header. Changing the token signs everyone out.
- **Guessing is impractical:** 0.5 s per wrong attempt and a lockout after 10 a
  minute, refusing even the correct token, so a guesser gains nothing.
- **Fail closed:** the hub will not start with `VERIFY_SIGNATURES` on and no token, or
  with one under 16 characters, and Compose fails before starting. API docs are
  hidden when auth is on.

**Found by testing:** the route audit passed vacuously, because this FastAPI wraps
included routers and `app.routes` showed none of them. It now asks `app.openapi()`
for the real routes, hits each anonymously, demands 401, and asserts it found enough
of them to mean something.

**Not solved:** one secret means no per-user accounts and no record of who did what,
and the cookie is only `Secure` behind HTTPS. Passkeys (tier 3) remain the plan.

---

## 43. Stack problems resolve themselves, strictly

**Decision:** a stack problem's issue resolves after 10 minutes of reports in a row
with the problem absent (`STACK_HEALTHY_MIN`).

**Why so strict:** a resolved issue that relapses becomes a regression, which is an
alert once chunk 2 exists. A flapping stack must therefore not resolve and reopen
all day. The window needs history reaching back far enough to vouch for all of it,
and any gap longer than the stale threshold blocks it, because a silent reporter is
not evidence of health (#10).

**Boundaries:** only `open` issues are touched. Muted and snoozed stay as they are,
and one a person resolved is left alone. Each resolve is logged as automatic.

**Found by testing:** the tolerance for short history was 90 seconds, which is most
of a 2-minute window, so it now scales with the window.

---

## 44. A media archive is a planned side project, and it stays off the staging volume

**Decision:** plan a film archive as a side project, documented in
`docs/MEDIA-ARCHIVE.md`, and do not build any of it yet. Nothing else waits on it.

**Where it lives:** on its own Raspberry Pi with its own disk, fully decoupled from the ThinkCentre. Sharing
a machine with staging was considered and rejected: a full media disk would take Postgres, Docker and Gitea down
with it, and it would need a priority gate between media and deploys. Decoupling removes the problem, so the
archive doc makes no comparison with the ThinkCentre and has no gate.

**What Nightjar does:** watch, not manage. Its job stays narrow, and tools for storing
and serving footage exist. Media problems are P2 or P3, never P1, so they cannot wake
you.

**Research honesty:** the storage-size numbers were checked against Apple's own tables.
The first search summary had ProRes 422 HQ UHD 30p wrong (629 Mb/s), and reading the
source corrected it to 884 Mb/s. Camera-native bitrates, Pi transcoding and Samba speeds
are marked as estimates or community-reported in the doc. Six open questions (Pi models,
the archive disk, camera formats, where it lives, how files arrive, a second copy) must
be answered before any of it is built.

---

## 45. A client integration kit fixes the two limits of the probe

**Decision:** plan push checks and heartbeats for apps the hub cannot reach, an add-site
form and a monitoring warning for the "forgot to set the URL" failure, and a `nightjar`
CLI, libraries and a dependency-free shell script to make installing on clients easy.
Documented in `docs/CLIENT-INTEGRATION.md`. Nothing is built.

**Two mechanisms, deliberately:** a push check is the operator measuring the app from
outside (so it gives an uptime ring and latency); a heartbeat is the app saying it is
alive, and alerts on silence (so it catches a dead host). They reuse the existing
pipeline: results go into `checks`, silence shows as `probe silent` and never as
healthy (#10), issues are edge-triggered and resolve strictly (#38, #43), and severity
is derived from what the heartbeat is, never chosen by hand.

**#20 still holds.** The packages wait for three or four apps. What is not a package,
the hub endpoints, the form, the warning and `nightjar.sh`, is small and worth doing
early.

**The trade-off accepted, stated plainly:** a published package runs with every
client's site secret, so one compromised release exposes them all. That is why the plan
requires zero runtime dependencies, trusted publishing with provenance, exact-version
pinning by clients, ingest-only credentials (the console token never goes in a client's
CI), and a vendorable script for anyone who would rather read than trust.

**Checked, and what was not:** plain `nightjar` is taken on npm, so the package is
scoped. Ownership of `@brandon` is unverified (an account can own a scope with nothing
published, and the profile page blocks automated checks), so the scope must be chosen and
created first. `nightjar-reporter` is free on PyPI. Trusted publishing supports GitHub
Actions and not self-hosted runners such as the Gitea one, so packages publish from GitHub.

---

## 46. On location, media is saved locally first and uploaded once the local copy is confirmed

**Decision:** plan the shoot-day workflow as two storage tiers. Tier 1 is a fast SSD on a
field Pi, which takes the card offload. Tier 2 is the archive disk on the home Pi. A file is
copied to tier 1 and verified by read-back, and only then may its card be cleared. It is
uploaded to tier 2 only once tier 1 is confirmed, and freed from tier 1 only once tier 2
verifies. Documented in `docs/MEDIA-ARCHIVE.md`. Nothing is built.

**Why:** the scarce thing on a shoot is empty cards, and the fast path to one is a local
copy. LTE carries a few to a few hundred Mbps at best, against hundreds of GB per hour of
RAW, so any design where freeing a card waits on an upload would stop the shoot. Separating
"clear the card" from "upload" also means the slow link can fail without costing a shot.

**Rejected:** clearing cards against the remote copy (LTE cannot keep up); a hard disk as
the field tier (about four times slower to clear a card, worse when carried, and many large
portable disks are SMR); uploading everything over LTE (over a day per shoot at peak rates);
letting the software format cards (a person formats, after seeing "verified").

**Kept from before:** media problems are P2 or P3 and never P1, Nightjar only watches, and
the media Pis are decoupled from staging and deploys, so nothing needs gating.

**Not verified:** the SSD and hard disk throughput and the card-clearing times are working
estimates, the GB per hour for ProRes RAW is inferred from Apple's chart, and neither the
modem's category nor the real LTE speed on site is known. Five new questions are in the
design doc.

---

## 47. Home streaming runs on the ThinkCentre, is watched by Nightjar, and its history is kept

**Decision:** Jellyfin runs on the ThinkCentre with its library on an external SSD, for about two concurrent
viewers of ordinary TV shows. Nightjar watches it: a reporter next to Jellyfin pushes its state to the hub
(`hub/media.py`, `client/jellyfin_reporter.py`), the console has a **Media** tab, and the idle screen has a panel.
Documented in `docs/MEDIA-ARCHIVE.md`.

**Why on the ThinkCentre:** the operator chose it. The rejected worry was contention with staging. Production
runs on the VPS, so contention can only slow staging builds, and a CPU limit on the Jellyfin container contains
that. This does not reverse #44 for the archive Pi, which stays a separate system.

**What is shown:** the console shows names and titles, because it sits behind the console token. The idle screen
always shows the server's state, the number of streams and the disk, and hides who and what until the fingerprint
reveal, the same rule as incident titles (#13).

**History is kept**, by the operator's choice: one row per viewing with the user, title, device, play method and
time actually watched. It is personal data, so it is pruned after `MEDIA_HISTORY_DAYS` (default 365) and is shown
only in the console, never on the wall.

**Kept:** the hub never reaches into the home network (the reporter pushes), the Jellyfin API key never leaves the
media machine, silence is `reporter silent`, problems are P2 edge-triggered issues that resolve themselves, and a
stream starting never alerts.

**Not verified:** field names against a live Jellyfin, and hardware transcoding on this CPU.

---

## 48. The wall shows a clock with seconds, and the weather and altitude of where it hangs

**Decision:** the idle screen's clock shows hours, minutes and seconds, and beside it the temperature,
condition, place and altitude. The console's sidebar carries a one-line version. The hub serves it at
`/api/weather` (`hub/weather.py`).

**Why Open-Meteo, not AccuWeather:** the operator suggested AccuWeather. Its free tier is small (reported as
50 calls a day, with a location lookup costing a call of its own) and needs a key. Open-Meteo needs no key,
allows up to 10,000 calls a day for non-commercial use, returns the terrain elevation in the same response,
and is licensed CC BY 4.0. The hub calls it once per `WEATHER_TTL_MIN` (15) however many screens are open. If
AccuWeather is wanted anyway, `_fetch` and `parse` in `hub/weather.py` are the only two places that know the
provider.

**Weather is decoration, and can never be an alert or a fault.** The endpoint always answers 200. A failed
fetch keeps the last good reading and marks it `old` once it is three intervals stale; with no reading at all
it says "weather unavailable". It is deliberately not one of the feeds that decide "hub unreachable".

**The altitude is the terrain model's value at the configured coordinates** (a 90 m grid), not a survey. Set
`WEATHER_LAT` and `WEATHER_LON` to the exact spot. The default is central Nairobi, inferred from the
Africa/Nairobi timezone; the operator has not confirmed it.

---

## 49. Status icons in the wall's header: home health, internet, backup, presence, cabinet temperatures and the rate

**Decision:** small status icons sit in the idle screen's header, to the right of the brand and before the weather,
like a phone's notification area: an icon with a live rolling number beside each, instead of cards. They are
internet speed, backup age, home or away, intake air, exhaust air, the ThinkCentre CPU and the USD to KES rate
(on its own). Hovering or focusing an icon shows a small tooltip saying what it is and giving its numbers. Items
are dim while all is well, amber or red only for a real problem, and a silent reporter is an amber icon with a
dash, never a healthy number.

**It is not a separate bar.** Two earlier versions were rejected by the operator: a row of cards at the bottom,
and then a full-width pill bar across the top. The header is a three-column grid (brand, icons, weather and clock).
The icon column has a minimum width and wraps inside itself, so at 1366 px it becomes two tidy rows and can never
be drawn over the weather. Below 1350 px the weather's air column gives way first. The dashboard gets a Home tile
with the same numbers and a manual home or away switch.

**Air is part of the weather block**, not a tile: barometric pressure with its three-hour trend, and today's UV
peak, sit in a column beside the temperature, place and coordinates. The condition line reads "Rain likely now" with
the chance when the next hour says so, and otherwise just states the conditions. Rain expected later is not shown.

**Intake and exhaust temperatures** need the cabinet's own sensors, which do not exist yet (the design targets
"intake within 5 C of room ambient"). The reporter reads any Linux sensor file (a DS18B20's `temperature` or
`w1_slave`, or a hwmon input) named by `NIGHTJAR_INTAKE_SENSOR` and `NIGHTJAR_EXHAUST_SENSOR`. Without them those
two icons are absent, not zero. Only the CPU has thresholds (80 amber, 85 red); nothing in the design says what
intake or exhaust temperature is too warm, so they are shown and never coloured.

**Chosen by the operator** from a longer list: home health, rain and UV, backup, overdue requests, presence and
the rate. Sun times, certificate expiry and a calendar line were left out.

**Home health and the speed test** come from `client/home_reporter.py` on the ThinkCentre, pushed to
`POST /ingest/{key}/home`. The test runs **hourly with a 5 MB download and a 1 MB upload** (about 6 MB, roughly
5 GB a month), reduced from the 35 MB every 30 minutes first proposed, because the operator asked for less data.
The trade-off is accuracy: small files understate a fast line, so the number means "is the internet working and
roughly how well". Cloudflare's endpoints refuse Python's default user agent (the upload answers 403), so the
reporter sends its own. If home internet is down the reporter cannot reach the hub at all, and the console shows
it as silent.

**Problems** use the shared edge-triggered logic (`hub/edge.py`, extracted from the media code): `HomeHostHot`
(85 C) and `HomeDiskLow` (under 10%) are P2, `BackupStale` (36 hours, or tracked and never run) is P3. None is
P1. A backup path that is not configured is shown as "not tracked", which is different from "tracked and missing".

**Presence** is built without the alert policy: `POST /presence` (a token-checked NFC tap, 14 hours by default)
and the console's own switch. It is stored as taps with an expiry, and an expired tap or no record at all is
**away**. Nothing reads it yet, so it cannot suppress an alert; chunk 2's routing will.

**Client requests are not a card.** The operator wanted a subtle cue: an inbox icon and one number in the Active
incidents heading. It shows nothing when the inbox is clear, the open count in dim when requests are in time,
and the overdue count in amber, breathing, when any are late.

**Errors rising is red, with a red X when nothing is climbing.** Green was already staging deploys, and the calm
state's text stays neutral so a quiet card does not read as an alarm. This bends the "red means wake me" rule,
which the operator accepted.

**Weather additions:** rain chance, UV and pressure come from the same Open-Meteo call as the temperature.
Pressure is shown at sea level (what barometers and weather maps show), with a rising, falling or steady
trend over three hours. At this altitude the station itself reads about 840 hPa, kept in the tooltip.

**Sample data:** until the home reporter is connected, its three tiles show tagged `demo` sample values, as the
media tile does. The presence, rate and weather tiles are always real.

### 49a. The internet icon shows what the line is doing, temperatures have ranges, and the rate cycles

**Internet icon.** It is no longer static. Offline (a red crossed wifi) means the wall cannot reach the hub,
so the home connection is probably down. A silent reporter is an amber crossed wifi. Otherwise it follows the
last minute of traffic: a plain wifi when idle, a cloud with an arrow down when downloading, up when uploading,
and up-down arrows for both, each nudging in its direction. Traffic comes from the home reporter, which works
out bits per second from `/proc/net/dev` between its runs (ignoring loopback, Docker, veth pairs and Tailscale,
which would count bytes twice) and reports it as `rx_bps` and `tx_bps`. It is a one-minute average, so this
says "busy lately", not a live meter, and the hourly speed test shows up in it while it runs. Above 0.2 Mbps
counts as active. While the reporter is not connected, the sample data cycles through every state every 4
seconds so the icons can be seen.

**Temperature ranges** (`TEMP_RANGES` in `console/src/home.js`, one place to change): cool (blue) below the
first figure, operating (amber) up to the second, high (red) from it. CPU 40 and 80, intake 20 and 35,
exhaust 28 and 45. These are working figures, not measurements. The CPU's 80 is the cabinet design's own
sustained target; the air ranges are reasoned for a small PC in a warm room, and should be revisited once the
cabinet has been measured under load. The bar goes red at 80 but the hub only opens a P2 issue at 85.

**Exchange rate** cycles US dollar, pound, yuan every 5 seconds, each as Kenyan shillings for one unit. The hub
derives them from the one open.er-api.com call (a pound costs KES / GBP shillings) and leaves out any currency
the source did not quote rather than guessing.

**Unreachable hub.** The idle screen's incidents and errors-rising panels now say "Can't reach the hub" instead
of "All clear" and "No errors rising" when the hub cannot be reached. Silence must not read as all-clear, and
the panels had been claiming it.

### 49b. A staging push is its own card under the clock, and each currency has its own colours

**Staging push.** It began as a small icon in the header row, then the operator asked for it to be a card under
the clock showing each state in turn. It has four states: **loading** (a running deploy, with a progress bar),
**done** (the deploy reported success, so the files are delivered), **complete** (the staging stack for that site
then reported healthy, so it is live and working) and **failed**. "Done" and "complete" are different on purpose:
a finished upload says nothing about whether the stack it fed came up, so "complete" needs the stack's own report
made after the deploy as proof. A silent, stale or degraded stack, an older report, or another site's stack does
not count. Each state has its own time on screen: loading while the deploy runs (a running report over an hour old
is a CI job that never finished, and is dropped), done for 10 minutes if nothing confirms it, complete for 5, and
failed for an hour, because a failure needs seeing. The card slides in when there is something to say and slides
away after. The commit ref on it is blurred until the fingerprint reveal, like the deploys panels.

CI reports it through the existing `POST /ingest/{key}/deploy`, which takes an optional `progress` (0 to 100) on
`running` reports. Without it the bar slides, because a made-up percentage would be worse than none. Progress on a
finished deploy is dropped.

**The sample.** So the card can be judged, a sample push plays while no real one is running: always in development,
and in a real build only until a staging deploy has ever been reported. It walks through loading (12 seconds),
done (6) and complete (6), then the card goes away and returns after a pause that alternates 40 and 30 seconds
(`DEMO_*` in `console/src/push.js`), so it is on screen for under half the time. A real push replaces it.

**It may not disturb anything.** The card sits in the space under the clock, positioned over the verdict row rather
than in the flow, so its arrival cannot push anything. Measured in headless Chrome, with the card showing it ends
above the Active incidents panel at 1920 x 1080, 1600 x 900 and 1366 x 768 (it gives up its text line on shorter
screens, and shrinks further on the shortest, to stay inside that space). Its height is the same in every state.
The header's icon area is also locked to two rows of height, so a wrapping icon cannot make the header taller.

**The exchange rate** reads "$1 = 129.57 KES" (and "£1", "¥1"), the symbol as large as the number and "KES"
readable, after the first version's "KES/$" was too small to make out. Each currency has its own fill: dollar
green, pound a blue to red gradient, yuan red fading to white.

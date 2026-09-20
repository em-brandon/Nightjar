# Nightjar — start here

Incident hub for a solo developer maintaining client sites alongside a day job.
Crashes and client reports go in; a small number of well-chosen phone
notifications come out.

## Read in this order

1. **`CLAUDE.md`** — the software build. Claude Code reads this automatically.
   If you only read one file, read this one.
2. **`DEPLOY.md`** — getting onto a VPS.
3. **`INTEGRATE.md`** — wiring client apps, uptime checks, Sentry and Gitea in.
4. **`docs/DECISIONS.md`** — the history. Every choice, what was rejected, and
   why. Read this before reversing a decision; several were corrected once
   already and the reasoning is recorded.
5. **`docs/FULL-DESIGN.md`** — hardware, access tiers, later chunks. Nothing in
   here blocks anything. Read it when you get to it.

## Where the project actually stands

**Chunk 1 is done and tested.** A stack trace goes in, one deduplicated issue
comes out. 200 identical crashes with varying IPs and retry counts collapse to a
single row with `event_count` in the 200s.

**Chunk 3 is done.** A React console shows sites and open issues, polling every
30 seconds. See "Running it" in `CLAUDE.md` — it needs only Docker and Node.

**Chunk 2 is not written, and is deferred until there is money for it.** Pushover
is a paid licence, and production needs a domain and a VPS. This means Nightjar
currently *cannot tell you anything*: it dedupes, and you can look at the result,
but nothing interrupts you. Do not rely on it to catch a client outage yet.

Everything below is still the right order. It is paused on cost, not on design.

## Do these three things, in this order (when you can pay for them)

**1. Prove Pushover works. Ten minutes, tonight.**

Install the app, create an account, register an application, grab the user key
and app token. Then:

```bash
curl -s -X POST https://api.pushover.net/1/messages.json \
  -d token=APP_TOKEN -d user=USER_KEY \
  -d title="nightjar" -d message="wiring check" \
  -d priority=2 -d retry=60 -d expire=600 -d sound=persistent
```

If your phone wakes and keeps retrying until you acknowledge, the whole design
holds. If not, better to know now than after building a hub around it.

Add Pushover to your Focus mode allowlist, or P1 retries wait politely until
morning.

**2. Buy the domain and point nameservers at Cloudflare. Fifteen minutes.**

DNS propagation is the only step you cannot hurry, so start its clock while you
write code.

**3. Deploy chunk 1 to the VPS.** Follow `DEPLOY.md`. Confirm 200 crashes
collapse to one issue over HTTPS, then resolve it and send it again — a
`"regression"` response proves the state machine works end to end.

**Then write chunk 2.** It is one evening and it is the entire point.

## Mockups

`mockups/` holds working HTML you can open in a browser:

| File | What |
|---|---|
| `nightjar-console.html` | Boot splash → dashboard → issue drawer. Click any row |
| `nightjar-wall.html` | The mounted screen: tier 1 ambient, tier 2 fingerprint |
| `nightjar-boot.html` | Kiosk splash on its own |
| `nightjar-detail.html` | The drawer on its own |

These are design references, not the implementation. Chunk 3 builds the real
thing in React against the same layouts.

## Hardware

Deliberately not on the critical path. Nightjar lives on the VPS permanently —
a watchdog must not share a failure domain with what it watches. The
ThinkCentre, when it arrives, takes staging, Gitea, builds, backups, a second
uptime probe, and the wall display.

Cabinet drawings are in `assets/`, with generators in `tools/`. Before anything
is fabricated: run the tissue test on the chassis vents, and take an open-air
thermal baseline. Neither can be recovered after the fact.

## The one rule that governs everything

Presence, fingerprint, passkey, wall display, speaker, status light — **none of
them may ever suppress an alert.** Each may add a channel or reveal detail. If
every one fails at once, Pushover still fires.

The phone is load-bearing. Everything else is decoration on top.

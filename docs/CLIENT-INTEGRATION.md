# Client integration kit (planned)

> **Status: planned, not built.** Nothing here is in the code. This extends **chunk 4b** (fleet
> onboarding) and the standing decision to publish packages once there are three or four apps (#20). It
> is written down now so the small, useful parts can be built early and the publishing decision stays
> deliberate.

## The problem

Two limits of how Nightjar knows a client's app is up:

1. **The probe cannot reach a private app.** The hub, on the VPS, requests the site's `url` every minute. An
   app behind a firewall or on a private network cannot be reached that way.
2. **A site is not monitored until someone sets its `url`.** Today that is a hand-run `UPDATE`, and forgetting
   it leaves a site with no uptime ring and nothing to say so loudly.

Both get harder as the number of client apps grows, which is the point of chunk 4b.

## The kit at a glance

| Piece | What it does | Fixes | Built |
|---|---|---|---|
| **Push check** | A small job inside the client's network fetches the private URL and pushes the result to the hub | Unreachable apps still get an uptime ring, latency and a 90-day ribbon | No |
| **Heartbeat** | An app or cron job pings the hub every N minutes. The hub alerts when the pings stop | Detects a dead host or network, which a probe from inside cannot | **Yes**, with a console tab (see below) |
| **Monitoring warning** | A site with no `url` and no pushed checks is flagged on the Overview | "Forgot to set the URL" | No |
| **Add-site form** | Create a site and set its URL from the console, no SQL | Setup friction | API yes (`POST /api/sites`, `PATCH /api/sites/{id}`); form no |
| **`nightjar` CLI** (npm) | `deploy`, `heartbeat`, `check` for CI and cron, and `add-site` for the operator | Installing on each client | No |
| **Node, PHP and Java reporters** | One dependency-free file each: `client/node`, `client/php`, `client/java` | Non-Python apps had no reporter | **Yes**, copied in. See [`REPORTERS.md`](REPORTERS.md) |
| **Packages on npm and PyPI** | The same files published, so a fix is one `update` | Editing N copies | No: still copying, until the third or fourth app |
| **Browser reporter** | `client/browser/nightjar.ts`: uncaught page errors, with a public key and no secret | Sites with no backend (Bird Box) and every site's frontend | **Yes**, see [`KEYS.md`](KEYS.md) |
| **`nightjar.sh`** | The same calls in POSIX shell with `curl` and `openssl`, no runtime needed | Hosts without Node or Python, and anyone who wants to read every line | **Yes**: `client/nightjar.sh` has `heartbeat` and `deploy`. Not yet `check` (the push check is unbuilt) |

### Push check versus heartbeat

They answer different questions, and private apps usually want both.

- **Push check:** *the operator measures the app from outside.* A job on any machine that can reach the app
  requests its health URL and reports `ok`, the status code and the latency. It reuses the existing `checks`
  table, so the same ring, latency and ribbon appear, and the same rule applies: if the reports stop, the site
  shows `probe silent`, never healthy. It only works while the machine running the check is alive.
- **Heartbeat:** *the app says it is alive.* The alert fires on **silence**, so it catches a dead host, a dead
  network and a stuck cron job. It says nothing about whether the app is serving correctly.

### Planned shape of the endpoints

Both authenticate like every other ingest, by the site's key and the HMAC signature.

```
POST /ingest/{key}/check                {"ok": true, "status_code": 200, "latency_ms": 143}
POST /ingest/{key}/heartbeat/{name}     optional {"status": "ok" | "fail", "message": "..."}
```

A heartbeat has a **name**, an expected **interval** and a **grace** period. Severity is derived from what the
heartbeat *is*, never chosen by hand: a heartbeat standing in for uptime is P1, a scheduled job is P2. Missing
pings open an issue **on the edge**, once, and it resolves itself after the pings return and stay, using the
same rules as staging stacks (#38, #43).

**Heartbeat: built** (`hub/heartbeats.py`, the console's Heartbeats tab, `client/nightjar.sh`). The push check is not. A heartbeat is
registered by its first ping, which carries its schedule, so the cron line is the one place that says how often it runs:

```bash
# at the end of a successful run (signed with NIGHTJAR_SECRET; needs only curl and openssl)
0 3 * * *  /opt/backup.sh && nightjar.sh heartbeat nightly-backup --every 1d
# a run that failed: the job is alive but broken, which opens its own incident
0 3 * * *  /opt/backup.sh || nightjar.sh heartbeat nightly-backup --every 1d --fail "backup.sh exited $?"
# a private app that stands in for uptime: a production app that goes silent is a P1
* * * * *  nightjar.sh heartbeat kubasu-api --every 1m --kind app
```

`nightjar.sh` reads `NIGHTJAR_URL`, `NIGHTJAR_KEY` and `NIGHTJAR_SECRET`, refuses a plain `http://` hub, and like every reporter
**never breaks the job it follows**: a hub that is down is one stderr line and exit 0 (`NIGHTJAR_STRICT=1` to see failures while
setting up). The raw call is `POST /ingest/{key}/heartbeat/{name}` with an optional JSON body, signed like any ingest.

| Field | Default | Meaning |
|---|---|---|
| `interval_s` | `HEARTBEAT_DEFAULT_INTERVAL_S` (300) | how often it promises to ping, 30 s to 7 days |
| `grace_s` | a fifth of the interval, between 1 minute and 1 hour | how late is still on time |
| `kind` | `job` | `app` stands in for uptime (P1 in production); `job` is P2. Staging is never P1 |
| `status` | `ok` | `fail` opens its own issue even though the job is alive |

It opens **one** issue when pings stop (checked each probe round, edge-triggered), and resolves itself after three good
pings in a row, or one for a heartbeat slower than hourly (a nightly job cannot flap, and three would leave it open for three
days). **A heartbeat that has never pinged does not exist yet**, so it cannot be missed: run it once by hand after wiring it.
`GET /api/heartbeats` lists them and `DELETE /api/heartbeats/{id}` retires one on purpose. The console shows `ok`, `late` (past
its interval, inside its grace, nothing alerted), `missed`, `failed`, and `recovering` (pinging again, incident still open).

**Broken for real on 2 November 2026**: a real hub process, a real database and a derived site secret, pinged through
`nightjar.sh` with `--every 30s --grace 10s --kind app`, then left silent. `ok` at 20 s, `late` at 35 s, and at 45 s a **P1 was open
with one event**, which stayed one event for the rest of the silence, with one P1 notification queued. Pinging again, the state
read `missed` after the first and second good pings (a bug, now `recovering`) and the incident resolved on the third, logged as
automatic. So the time from the last ping to an alert is the interval, plus the grace, plus up to one probe round (60 s by
default).

**What that means for the defaults.** They are reasonable, but they come from the arithmetic and that one run, not from a
client's traffic, so treat them as a starting point to tighten after the first false alarm or the first slow one:

| Heartbeat | Suggested | Alert arrives after silence of about |
|---|---|---|
| Live private app, production (`app`) | `--every 1m` | 3 minutes (1 interval, 1 grace, up to 1 probe round) |
| A worker or queue consumer (`job`) | `--every 5m` | 7 minutes |
| Hourly job | `--every 1h` | about 1 h 13 min |
| Nightly job | `--every 1d` | about 1 day 1 h 1 min |

The grace is the figure to widen first for a job whose run time varies (a backup that takes 20 to 50 minutes), since the ping is
sent when it finishes: `--grace 1h`.

Site creation moves behind the console token: `POST /api/sites`, used by the form and by `add-site`.

## How each reachability case is handled

| Situation | Piece | Notes |
|---|---|---|
| Ordinary public website | Existing probe | Set its `url`. Nothing else needed |
| Private app, host stays up | **Push check** from any machine that can reach it | The ThinkCentre over Tailscale, or a small box at the client |
| Private app, and you need to know if the host dies | **Heartbeat** | Outbound traffic only, which nearly every network allows |
| Cron job, background worker, batch task | **Heartbeat** | Sent at the end of a successful run |
| Client can open one port | Allowlist the VPS's fixed IP to a `/healthz` | Simplest, needs a firewall change on their side |
| Client will install Tailscale | Probe the private address directly | Corporate networks often refuse |

**What works today, with no new code:** a cron job on the client's host checks the private URL and, on failure,
posts a `fatal` event to `/ingest/{key}/event`. That raises a P1 through the normal pipeline. Its limit is stated
above: if the whole host dies, nothing is sent.

## Can this go on npm? Yes.

The plan already names `@brandon/nightjar`. What was checked, and how far to trust it:

| Question | Finding | Confidence |
|---|---|---|
| Is plain `nightjar` free on npm? | **No.** Taken by an unrelated maths library (v0.1.18, last published May 2021) | Checked on the registry |
| Is `@brandon` yours to use? | **Unknown.** No public packages exist under it, but an account can own a scope with nothing published, and the npm profile page is blocked to automated checks | **Unverified** |
| Is `@nightjar` free? | No packages under it. Whether the organisation name is available is not visible | Unverified. Try creating it |
| Is `nightjar-reporter` free on PyPI? | **Yes**, the project returns 404 | Checked |
| Where can it be published from? | npm **trusted publishing** supports GitHub-hosted Actions runners, GitLab.com and CircleCI. **Self-hosted runners are not supported**, and Gitea is not mentioned. It needs npm CLI 11.5.1 or newer and Node 22.14 or newer, and it generates provenance automatically | Read in npm's docs |

**Consequences:**

- The package must be **scoped**, and the scope must be your npm username or an organisation you own. An
  organisation is free for public packages and keeps the name off your personal account. **Decide the scope
  first**, then create it, because everything else follows from the name.
- Publish from **GitHub Actions**, where this repository lives, not from the Gitea runner on the ThinkCentre.
  That gives signed provenance and no long-lived npm token to leak. Client apps can still *consume* the package
  from their own Gitea CI, which is unaffected.
- Requiring Node 22 is for the *publish* step only. The package itself should run on older Node (the built-in
  `fetch`, Node 20 or newer recommended, which is my judgement and not tested).

## The risk to take seriously: it runs with your clients' secrets

A package installed in a client's CI or cron holds that site's ingest secret. If the package is ever
compromised, it can read the secret of **every client that installs it**. Supply-chain attacks on npm are common
enough to plan for.

| Mitigation | What it does |
|---|---|
| **Zero runtime dependencies** | No transitive packages to be poisoned. Only our own code runs |
| **Trusted publishing with provenance** | No stored npm token to steal, and each release is tied to a repository, commit and workflow |
| **Two-factor authentication** on the npm account | Blocks the takeover route |
| **Clients pin an exact version and use a lockfile** (`npm ci`) | An update reaches them only when they choose it. No `^` ranges |
| **A tiny, auditable source** | Small enough to read before installing |
| **`nightjar.sh`, vendorable** | A client who does not trust a package can copy one short script and read it |
| **One key pair per site** (already the design) | A leaked secret exposes one site, not all of them |
| **Ingest-scoped credentials only** | A site's key can report events, deploys, checks and heartbeats. It **cannot** read the console or mute an issue, because `/api` needs the console token. **That token never goes in a client's CI** |
| **The secret is never sent** | Requests are signed with HMAC. The secret stays on the client |

## Order of work

**Do first, because it is small and useful before there are four apps:**

1. The **monitoring warning** on the Overview.
2. `POST /api/sites` and the **add-site form**.
3. The **push check** and **heartbeat** endpoints, with their console display and issues. *Heartbeat done; push check not.*
4. **`nightjar.sh`**, which needs no publishing and unlocks private apps immediately. *Done for heartbeat and deploy.*

**Do at three or four apps, as decision #20 already says:**

5. The **`nightjar` CLI** and the **Node library**, and the **PyPI package**.
6. **`add-site`** in the CLI.
7. Publish through GitHub Actions trusted publishing.

Publishing free packages costs nothing. The cost is maintaining them: versions, a release step and keeping the
supply-chain rules above true.

## Open questions

1. **Which stacks do your clients run** (Node, PHP, Python, Java)? That decides which libraries are worth writing.
2. **Which clients are behind private networks**, and could any open one port to the VPS's IP?
3. **Which npm scope:** your personal account, or a new organisation?
4. **Heartbeat defaults:** what interval and grace suit a cron job versus a live app?
5. **Where should the CLI live:** in this repository as a workspace package, or in its own?

## Sources

- npm registry, for `nightjar`: [registry.npmjs.org/nightjar](https://registry.npmjs.org/nightjar)
- npm registry scope search: [scope:brandon](https://registry.npmjs.org/-/v1/search?text=scope:brandon&size=5), [scope:nightjar](https://registry.npmjs.org/-/v1/search?text=scope:nightjar&size=3)
- PyPI: [nightjar-reporter](https://pypi.org/pypi/nightjar-reporter/json) (404 when checked)
- [npm trusted publishing](https://docs.npmjs.com/trusted-publishers/) and [GitHub's announcement](https://github.blog/changelog/2025-07-31-npm-trusted-publishing-with-oidc-is-generally-available/)

# Deploying Nightjar to a VPS

Chunks 1–4 have no hardware dependency. This gets the ingest path live on a
public HTTPS endpoint so client apps can start reporting immediately.

The wall display (chunk 5) and the Pico companion (chunk 6) need the
ThinkCentre. Everything else runs here, and arguably should stay here
permanently — a watchdog on separate power and a separate ISP from the things
it watches is the correct arrangement, not a stopgap.

---

## 1. Pick a box

The workload is small: FastAPI, Postgres, Caddy. **2 vCPU / 4 GB** is
comfortable and leaves room for the console and email intake later. 1 GB
technically runs it but leaves nothing for a Postgres cache.

| Provider | Notes |
|---|---|
| Hetzner | Cheapest by a wide margin. Prices rose in June 2026 — CX23 is around €5.49/mo. EU only for the cheap tiers |
| DigitalOcean | Pricier, but better docs and a simpler console if this is your first VPS |
| Contabo | More RAM per shilling, slower disks, patchier support |

**Region:** latency to Nairobi barely matters — this thing receives webhooks and
sends pushes, it is not interactive. Falkenstein or Nuremberg is fine. If you
prefer lower RTT, a provider with a South Africa or UAE region will feel
snappier over SSH.

**Payment:** most providers want a card, not M-Pesa. Hetzner may ask for ID
verification on a new account, so allow a day.

---

## 2. Point DNS

Before you build anything, create an A record:

```
nightjar.yourdomain.co.ke  →  <vps ip>
```

Do this first. Caddy cannot issue a certificate until it resolves, and DNS is
the one step you cannot hurry.

---

## 3. Bootstrap the box

```bash
ssh root@<ip>
# paste your public key into a file first, e.g. /root/id_ed25519.pub
bash bootstrap.sh brandon /root/id_ed25519.pub
```

Creates a non-root sudo user, installs Docker, enables ufw (22/80/443 only),
disables root login and password auth, turns on unattended security upgrades.

**Reconnect as the new user and confirm it works before closing the root
session.** Locking yourself out of a fresh VPS is recoverable via console
access, but it is a bad way to spend an evening.

---

## 4. Deploy

```bash
sudo mkdir -p /opt/nightjar && sudo chown $USER /opt/nightjar
cd /opt/nightjar
# copy the repo up, or clone it
cp .env.prod.example .env
nano .env                        # set HUB_DOMAIN, and check POSTGRES_USER and POSTGRES_DB
./deploy/make-secrets.sh         # creates ./secrets: the database password, the console token, and the other secret files

docker compose up -d --build     # the first build downloads Node and compiles the console, so it takes a few minutes
docker compose logs -f caddy     # watch the certificate get issued
```

**Secrets are files, not environment variables.** `make-secrets.sh` writes one file per secret into `./secrets` (mode 700, never
committed): `postgres_password`, `database_url`, `console_token`, `secret_master_key` and `pushover_callback_token` are generated, and
`pushover_token`, `pushover_user`, `deadman_url` and `presence_token` are created empty for you to fill in. Compose mounts them under
`/run/secrets`, so `docker inspect`, process listings and crash dumps never show them (checked: no container's environment holds a
secret). It never overwrites a file that exists, so running it again is safe. **Back the directory up somewhere other than the
server**: it is not in git, and the console token inside it is what you sign in with. Keep `secret_master_key` in your password manager
too: every new site's ingest secret is worked out from it and none is stored, so a database backup alone cannot restore them (see
`docs/KEYS.md`). The hub refuses to start in production without it.

The hub migrates its own database on every start, so there is no schema step. It also refuses to start
if the console token is missing, empty or under 16 characters, because otherwise the console API and every write
would be open to the internet (checked: an empty `secrets/console_token` makes the hub exit with an error). Sign in at your domain with that token. The session cookie is marked
`Secure` behind Caddy's TLS, so never use the console over plain http.

**Caddy serves the console too.** `deploy/caddy.Dockerfile` builds the console and bakes it, with the Caddyfile, into the Caddy
image, so one hostname serves the console, the API it calls, and the endpoints other machines call (`/ingest`, `/healthz`,
`/pushover/ack`, `/presence`). After changing the console or the Caddyfile, rebuild it:
`docker compose build caddy && docker compose up -d caddy`.

**HSTS starts at one day** (`max-age=86400` in `deploy/Caddyfile`). It tells browsers to use HTTPS only for this hostname and is
hard to undo, so raise it in steps: after a day with nothing broken, a week; after that, six months. Do not add
`includeSubDomains` or `preload` until every subdomain of the domain serves HTTPS. Check it with
`curl -sI https://<hub domain>/ | grep -i strict-transport`. The responses also carry `nosniff`, no framing, no referrer and a
Content-Security-Policy that lets the console load only its own files.

Then create the first site and note its keys:

```bash
docker compose exec hub python seed.py
```

---

## 5. Prove it

```bash
curl https://nightjar.yourdomain.co.ke/healthz
```

Then run the dedupe test against the live endpoint. It sends 200 varied copies of
one crash, and **PASS** means they landed in one issue:

```bash
NIGHTJAR_URL=https://nightjar.yourdomain.co.ke NIGHTJAR_SECRET=<ingest_secret> \
  ./test_ingest.sh <ingest_key>
```

`VERIFY_SIGNATURES` is `true` in production, so unsigned requests are rejected.
The script signs each request with the site's `ingest_secret` (from `seed.py`), the
same way `client/reporter.py` does, so there is no need to switch the check off to
test. If you leave the secret out, it says that is why it failed.

---

## 6. Backups, from day one

```bash
crontab -e
15 2 * * * /opt/nightjar/deploy/backup.sh >> /var/log/nightjar-backup.log 2>&1
```

Backups are encrypted with [age](https://github.com/FiloSottile/age) to a **public** key, so the VPS can write
backups but not read them. The dump holds every site's ingest secret, so this matters.

```bash
# On your laptop, not the VPS:
age-keygen -o nightjar-backup.key        # prints the public key (age1...)
```

- Put the **public** key in the VPS's `.env` as `BACKUP_AGE_RECIPIENT=age1...`. Install `age` on the VPS (`apt install age`).
- Keep `nightjar-backup.key` in a password manager, **not on the VPS**. Lose it and every backup is unreadable, and you
  only find out on the day you need one.
- Without `BACKUP_AGE_RECIPIENT` the script exits with an error instead of writing a readable copy.

Set `RCLONE_REMOTE` in `.env` to push to Backblaze B2 or Cloudflare R2. A
backup that lives only on the machine it protects is not a backup.

Restore-test it once, now, while there is no data you would miss:

```bash
deploy/restore.sh nightjar-<stamp>.sql.gz.age nightjar-backup.key                       # proves the file and key work
deploy/restore.sh nightjar-<stamp>.sql.gz.age nightjar-backup.key postgresql://...      # into a clean database
```

---

## 7. The heartbeat

Same rule as ever: **the hub going silent must not look like everything being
fine.** Create a check on healthchecks.io and have the VPS ping it every five
minutes:

```
*/5 * * * * curl -fsS -m 10 --retry 3 https://hc-ping.com/<uuid> > /dev/null
```

Better, and built: set `DEADMAN_URL` in `.env` to the same ping URL and the hub's alert sender pings
it after every completed pass (at most every two minutes). That proves the whole path is alive, database
round-trip and sender included, and not just that cron ran. A sender that has died, stuck, or lost its
database stops pinging, and healthchecks.io tells you. Keep the cron line too: it notices a hub that is not
running at all, which the in-app ping cannot.

---

## 7b. Phone alerts (Pushover)

Nothing wakes your phone until this is done. Without it every alert is queued and then shown as **failed**
in the console, never dropped.

1. Install the Pushover app on the phone (30-day trial, then a one-off licence per platform).
2. Create an application at <https://pushover.net/apps/build>, named `Nightjar`. Its API token goes in
   `PUSHOVER_TOKEN`. Your own user key, from the Pushover dashboard, goes in `PUSHOVER_USER`.
3. For acknowledging a P1 on the phone to reach the console at once, set `HUB_PUBLIC_URL` to the hub's public
   address and `PUSHOVER_CALLBACK_TOKEN` to a long random string (`openssl rand -hex 24`). Pushover cannot
   reach a hub on `localhost`; without these the hub checks Pushover for acknowledgements every 30 seconds.
4. Restart the hub, open the console's **Alerts** tab, and press **Send a test push**. It should arrive on
   the phone within seconds. If it does not, the tab says why.
5. Test it for real, as `CLAUDE.md` says to: break a route on staging and confirm the push arrives, then
   test presence by tapping out and confirming the push still arrives.

What the tab shows, and what each warning means:

| Shown | Meaning |
|---|---|
| Pushover **not configured** | The token or user is missing. Nothing can reach the phone. |
| Sender **stalled** or **switched off** | The loop that delivers alerts is not running. Alerts are queued and wait. |
| Alert **failed** | Pushover refused it, or it ran out of attempts. It has a **Retry** button. |
| P1 **not acknowledged** | It was sent but nobody answered within 30 minutes: the phone did not wake anyone. |
| **Stopped: incident dealt with** | You acknowledged, resolved, muted or snoozed the incident, or it healed, so the hub told Pushover to stop the repeating push. |

Acknowledging or resolving an incident in the console stops the phone alarming within a few seconds, and a failed attempt to
stop it is retried until it works. Nothing yet escalates a P1 that nobody answers: see `docs/ESCALATION.md`.

---

## When the ThinkCentre arrives

Nothing moves. The VPS keeps Nightjar. The Tiny becomes:

- staging environments and build host (isolated from the hub, so a runaway
  build cannot degrade alerting)
- Gitea, backup target
- a **second** uptime checker on a different network — which lets you tell
  "the site is down" apart from "my internet is down"
- the wall display and the Pico companion device

---

## Staging host: control and reporting

Staging runs on the ThinkCentre, one Compose project per client under
`/srv/staging/<client>/`. Nightjar's hub is on the VPS, so it splits two jobs
that must not be mixed:

| Job | Where | How |
|---|---|---|
| **See** the stacks | Nightjar's Staging tab | A reporter on the ThinkCentre *pushes* state in |
| **Control** them (start, stop, logs) | Dockge on the ThinkCentre | Reached over Tailscale, never the public internet |

The hub never reaches into the ThinkCentre and is never handed its Docker.
Everything is in [`deploy/staging/`](deploy/staging/).

### Control: Dockge over Tailscale

1. Install Tailscale on the ThinkCentre and on the devices you will use.
2. `tailscale ip -4` on the ThinkCentre, and put it in `deploy/staging/.env` as
   `TAILSCALE_IP=100.x.y.z`.
3. `docker compose -f dockge.compose.yml up -d`, open `http://100.x.y.z:5001`, and
   create the admin account **immediately**.
4. Set `VITE_STACK_CONTROL_URL` in the console's `.env` to that address, so the
   Staging tab shows a **Manage stacks** button.

Three things worth knowing:

- **The Docker socket is root-equivalent.** Anyone who can use Dockge can run
  anything on that machine. That is why it stays on the tailnet and behind its
  own login, and why it is not exposed through Caddy.
- **Docker bypasses `ufw`.** The Firewall note below is true of the VPS, where
  nothing is published. On the ThinkCentre a plain `5001:5001` would be open to
  the whole LAN whatever `ufw` says, so the compose file binds to the Tailscale
  address only and refuses to start without one.
- **Portainer works too.** It is heavier and more general, and Dockge is a better
  fit for the one-directory-per-client layout. Coolify and Dokploy go further and
  also automate routing and deploys, at the cost of another system to maintain.

### Reporting: the stack reporter

[`client/stack_reporter.py`](client/stack_reporter.py) reports each Compose
project you list in `stacks.json` (project name to that staging site's ingest
key). Install `stack-reporter.service` and `stack-reporter.timer` from
`deploy/staging/` to run it every minute. Follow the steps in the service file.

- **Run it as a dedicated user in the `docker` group.** Reading `docker ps` needs
  the socket, which is root-equivalent. A read-only socket proxy would narrow this
  and is a reasonable later hardening.
- **Name the staging site `<production>-staging`.** That is how the Staging tab pairs it with the production
  site to compare refs and judge whether it can be promoted.
- **Disk space** is read from the filesystem Docker writes to (`/var/lib/docker` if present). Set
  `NIGHTJAR_DISK_PATH` in `stack-reporter.env` if yours lives elsewhere.
- **Every service in a stack needs a `mem_limit`.** The Staging tab flags any
  running stack without one. See `client-stack.example.yml`.
- **If the reporter dies**, the tab shows the stack as `reporter silent` rather
  than healthy, and drops it from the totals.

Check it with `python stack_reporter.py` by hand, then open the Staging tab.

---

## Firewall note

`ufw` allows only 22, 80 and 443. Postgres and the hub are not published to the
host at all — they are reachable only over the internal compose network, with
Caddy as the single ingress. Do not add a `ports:` mapping to `db` "just to
check something with a GUI"; use `docker compose exec db psql` or an SSH tunnel.

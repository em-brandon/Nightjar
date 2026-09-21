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
openssl rand -base64 32          # paste as POSTGRES_PASSWORD
openssl rand -base64 32          # paste as CONSOLE_TOKEN (the hub will not start without it)
nano .env                        # set HUB_DOMAIN too

docker compose up -d --build
docker compose logs -f caddy     # watch the certificate get issued
```

The hub migrates its own database on every start, so there is no schema step. It also refuses to start
if `CONSOLE_TOKEN` is missing or under 16 characters, because otherwise the console API and every write
would be open to the internet. Sign in at your domain with that token. The session cookie is marked
`Secure` behind Caddy's TLS, so never use the console over plain http.

Then create the first site and note its keys:

```bash
docker compose exec hub python seed.py
```

---

## 5. Prove it

```bash
curl https://nightjar.yourdomain.co.ke/healthz
```

Then run the dedupe test against the live endpoint — 200 varied copies of one
crash must produce **one** issue:

```bash
./test_ingest.sh <ingest_key>      # edit URL to the https host
```

`VERIFY_SIGNATURES` is `true` in production, so unsigned requests are rejected.
`client/reporter.py` signs correctly; plain curl will need the HMAC header, or
flip the flag off briefly to test.

---

## 6. Backups, from day one

```bash
crontab -e
15 2 * * * /opt/nightjar/deploy/backup.sh >> /var/log/nightjar-backup.log 2>&1
```

Set `RCLONE_REMOTE` in `.env` to push to Backblaze B2 or Cloudflare R2. A
backup that lives only on the machine it protects is not a backup.

Restore-test it once, now, while there is no data you would miss.

---

## 7. The heartbeat

Same rule as ever: **the hub going silent must not look like everything being
fine.** Create a check on healthchecks.io and have the VPS ping it every five
minutes:

```
*/5 * * * * curl -fsS -m 10 --retry 3 https://hc-ping.com/<uuid> > /dev/null
```

Better, once chunk 2 exists, ping it from inside the app after a successful
database round-trip — then it proves the whole path is alive, not just that
cron ran.

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

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
nano .env                        # set HUB_DOMAIN too

docker compose up -d --build
docker compose logs -f caddy     # watch the certificate get issued
```

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

## Firewall note

`ufw` allows only 22, 80 and 443. Postgres and the hub are not published to the
host at all — they are reachable only over the internal compose network, with
Caddy as the single ingress. Do not add a `ports:` mapping to `db` "just to
check something with a GUI"; use `docker compose exec db psql` or an SSH tunnel.

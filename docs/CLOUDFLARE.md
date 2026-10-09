# Cloudflare checklist (free plan) and the move to nginx

> **Status: planned, not applied.** No Cloudflare zone exists for these sites yet, and the hub still runs behind Caddy.
> This is the checklist to follow when the domains are pointed at Cloudflare, written for the **Free plan**. It also
> records the planned switch from Caddy to nginx, because the two decisions affect each other.

## How much to trust this page

Each claim is marked by where it came from, because several Cloudflare limits are easy to remember wrongly.

| Mark | Meaning |
|---|---|
| **Checked** | Read in Cloudflare's own documentation while writing this (October 2026) |
| **Tested** | Run here: nginx 1.27 in Docker, in front of an echo server |
| **Unchecked** | From general knowledge, not confirmed. Verify before relying on it |

## A correction first

Earlier in this project I said a **WAF skip rule for the VPS's IP** would stop Cloudflare challenging the uptime probe.
**On the Free plan that is wrong for Bot Fight Mode.** Cloudflare's documentation says Bot Fight Mode "can't be skipped"
by WAF custom rules, Page Rules or Allow actions, because it runs in a separate pipeline. What does work is an **IP
Access Rule** that matches the request first, or keeping the hostname out of Cloudflare's proxy altogether. **Checked.**

## The short version

1. **The hub's own hostname is DNS-only** (grey cloud). Reports are small signed requests from servers and pages, and a
   challenge, a WAF block or a bot rule turns them into silence, because the reporters swallow failures by design.
2. **Client sites are proxied** (orange cloud), with SSL mode **Full (strict)**.
3. **Allow the VPS and the home IP** with IP Access Rules on every proxied zone, so Bot Fight Mode cannot challenge the
   uptime probe.
4. **Never cache `/api` or health routes.** Send `Cache-Control: no-store` from the app, and do not use "Cache Everything".
5. **Turn Always Online off**, or an outage can look like a working page.
6. **Prove it with a test report** that contains awkward text, through the real path, before trusting a site.

## What goes where

```mermaid
flowchart LR
    V["Visitors"]
    subgraph cf["Cloudflare, free plan"]
        EDGE["Proxied hostnames<br/>client sites<br/>Bot Fight Mode, WAF, cache"]
        MAIL["Email Routing<br/>support@ addresses"]
    end
    subgraph vps["VPS"]
        NGX["nginx<br/>TLS, limits, real IP"]
        HUB["Nightjar hub"]
        APP["Client backends<br/>e.g. Kubasu API"]
        PROBE(["Uptime probe"])
    end
    HOME["Home server<br/>staging, Tailscale only"]

    V --> EDGE
    EDGE -- "origin pull, Full strict" --> NGX
    NGX --> APP
    NGX --> HUB
    SRV["Client servers and CI"] -- "signed reports, DNS-only host" --> NGX
    PAGE["Visitors' browsers"] -- "page errors, DNS-only host" --> NGX
    PROBE -- "GET public URL" --> EDGE
    MAIL -. "to a Worker, then the hub<br/>not built" .-> NGX
    HOME -. "no public URL" .-> HOME
```

The probe goes **through** Cloudflare, so it measures what a visitor gets, including anything Cloudflare serves from
cache. Reports and the console go **straight** to nginx on a hostname Cloudflare does not proxy.

### Decide each hostname

| Hostname | Proxied? | Why |
|---|---|---|
| `nightjar.<domain>` (the hub: ingest, console, API) | **No, DNS-only** | Nothing here benefits from Cloudflare, and a bot rule or managed rule can silently drop a report |
| A client's public site | Yes | Cache, TLS, DDoS protection, which is what the free plan is for |
| A client's API on the VPS | Yes, with care | See "The Kubasu path" below. It must not be cached |
| Staging on the home server | Not at all | Reached over Tailscale, never on the public internet |

Put the hub on a domain **you** own, not under a client's zone, so a client's Cloudflare settings can never affect it.

## Checklist

### 1. Account and zones

- [ ] One zone per domain, all on the Free plan. **Checked:** custom WAF rules are limited to **5 per zone** on Free.
- [ ] Two-factor authentication on the Cloudflare account. It holds the DNS for every client.
- [ ] Keep a note of which zone belongs to which client in the console's client list.

### 2. DNS

- [ ] The hub hostname: an `A` record with the cloud **grey** (DNS only).
- [ ] Client site records: orange (proxied) once the site is live and answering.
- [ ] No record for staging. It has no public address.
- [ ] Lower the TTL a day before any move, so a mistake is quick to undo.

### 3. SSL/TLS

- [ ] **Full (strict)** on every proxied zone. **Checked:** Cloudflare "strongly recommends" Full or Full (strict). Do not
      use Flexible: it sends traffic to the origin unencrypted, and **Unchecked:** it is commonly reported to cause
      redirect loops when the origin redirects HTTP to HTTPS, which nginx will.
- [ ] **Certificates, per hostname:**
  - Proxied hostname: a Cloudflare **Origin CA** certificate installed on nginx is the simplest, but it is trusted only
    by Cloudflare. A Let's Encrypt certificate also works. **Unchecked:** the validity period of an Origin CA
    certificate. Look at the options when issuing one.
  - **DNS-only hostname (the hub): a public certificate is required**, because a browser connects straight to nginx and
    does not trust an Origin CA certificate. Use Let's Encrypt with `certbot`. This is the cost of leaving Caddy,
    which did this by itself.
- [ ] Redirect HTTP to HTTPS in nginx, not in Cloudflare, so the hub (which is not proxied) gets it too.

### 4. Bot Fight Mode, challenges and blocks

- [ ] **IP Access Rules (Allow)** for the VPS's address and your home address, on every proxied zone. This is the only
      way to keep Bot Fight Mode from challenging the uptime probe on the Free plan. **Checked.**
- [ ] Or turn Bot Fight Mode **off** on a zone whose site is only a brochure. It "may challenge API or mobile app
      traffic", and cannot be tuned on Free. **Checked.**
- [ ] Never enable **Under Attack** mode without remembering it will challenge the probe too.
- [ ] After setting a zone up, run the probe against it for a day and look for `HTTP 403` or `503` in the checks. A
      challenge page is a 403 or 503, which the console shows as an outage.

### 5. WAF custom rules (5 on Free)

- [ ] Do not spend rules on the hub: it is not proxied, so none apply to it.
- [ ] If a hub hostname ever has to be proxied: one rule with the **Skip** action for `starts_with(http.request.uri.path,
      "/ingest/")` that skips managed rules and rate limiting. **Checked:** Skip can skip managed rules and rate limiting
      rules, but not Bot Fight Mode.
- [ ] **Unchecked:** how the Free managed ruleset treats error text. Stack traces often contain SQL, file paths and
      `</script>`, which is exactly what attack rules look for. This is why the hub stays DNS-only, and why the test in
      step 12 uses awkward text.

### 6. Caching

- [ ] **Checked:** Cloudflare does not cache HTML or JSON by default, only by file extension (images, JS, CSS, fonts and
      so on). A `Cache-Control` of `private`, `no-store`, `no-cache` or `max-age=0`, a `Set-Cookie`, or any method other
      than `GET` also stops caching.
- [ ] Have the backend send `Cache-Control: no-store` on `/api/*` and on `/healthz`, so a rule added later cannot cache
      them by accident.
- [ ] **Do not use a "Cache Everything" rule on a zone that serves an API.** It overrides the default and can serve one
      visitor's response to another.
- [ ] **Checked:** **Always Online** exists on the Free plan and, when the origin is down, serves a stale copy or static
      parts from the Internet Archive, with a 30 day crawl interval on Free. That makes a dead origin return 200 and the
      probe say up. Leave it off. The page I read only describes turning it on, with a toggle in the domain's settings, so confirm the off switch there.

### 7. The real visitor address

- [ ] **Checked:** behind Cloudflare the socket address is Cloudflare's. The visitor is in `CF-Connecting-IP`, which
      Cloudflare recommends over `X-Forwarded-For` (that can hold a chain). `X-Forwarded-Proto` is set by Cloudflare to the
      protocol the visitor used, overwriting anything the visitor sent. nginx sets its own anyway (see below).
- [ ] On a **proxied** hostname, tell nginx to trust only Cloudflare's published ranges (`set_real_ip_from` for each,
      `real_ip_header CF-Connecting-IP`). **Do not paste the ranges into the repo.** They change. Fetch
      `https://www.cloudflare.com/ips-v4` and `ips-v6` in a scheduled job. **Unchecked:** this snippet has not been run
      here, only the DNS-only one below.
- [ ] On the **hub's DNS-only hostname**, trust nothing: the config below overwrites `X-Forwarded-For` with the socket
      address and clears `CF-Connecting-IP`. **Tested.** This matters because `hub/browser.py` reads `CF-Connecting-IP`
      first for its per-visitor limit, which a direct caller could otherwise fake.

### 8. Timeouts and sizes

- [ ] **Checked:** Cloudflare gives up on a slow origin after **125 seconds** (a 524) and limits URLs to 16 KB and
      headers to 128 KB. Nothing in Nightjar comes near them.
- [ ] **Unchecked:** the maximum upload size on Free. The documentation page I read did not state it. It only matters
      for a client app that takes large uploads.

### 9. Email (the planned `support@` intake)

- [ ] **Checked:** Email Routing is available on the Free plan and can send mail to a Worker as well as to an address.
- [ ] **Unchecked:** whether an Email Worker may call an outside URL. The page I read did not say. Workers can make
      requests in general, but test that the hub receives the mail before building the rest. Decision #7 depends on it.

### 10. The Kubasu path

Kubasu Motors is a React frontend on Cloudflare and a FastAPI backend on the VPS. Not started; these are the points to
settle first.

- [ ] Choose one origin or two: `kubasu.co.ke` for the frontend and `api.kubasu.co.ke` for the backend, or one hostname
      with `/api` routed to the VPS. Two is simpler and avoids a proxy rule.
- [ ] If two origins, the API must send CORS headers for the frontend's origin, and any cookie needs `SameSite` chosen on
      purpose.
- [ ] The API hostname is proxied only if the API sends `Cache-Control: no-store` (section 6).
- [ ] The API's own server errors go to the hub with a signed server reporter, and the frontend's errors with the
      browser reporter. Those talk to the hub's DNS-only hostname, not through the API.

### 11. Nightjar's own settings

- [ ] The probe URL for each client is a **`/healthz` path on the proxied hostname**, not the home page, so the check is
      cheap and never cached.
- [ ] Set `url` on the site only when the page is live (see the onboarding plan's "not launched" idea).
- [ ] `PROBE_INTERVAL_S` stays at 60. A shorter interval just trips more bot rules.

### 12. Prove it

Run these from the VPS, not from your laptop, so they see what the probe sees.

```bash
# 1. the probe's view: no challenge page, a real 200, and not served from cache
curl -sI -A "python-httpx/0.27" https://client.example.co.ke/healthz | grep -iE "^(HTTP|cf-cache-status|server)"
#    HTTP 200, cf-cache-status DYNAMIC or BYPASS. A 403 or 503 means a challenge: fix section 4.

# 2. the hub is reachable directly and Cloudflare is not in the path
curl -sI https://nightjar.example.co.ke/healthz | grep -iE "^(HTTP|server|cf-ray)"
#    HTTP 200, server nginx, and NO cf-ray header.

# 3. a report with awkward text goes through. Use the onboarding screen's test event when it exists.
```

## Switching from Caddy to nginx

**Status: planned.** The repository still uses Caddy: `deploy/Caddyfile`, a `caddy` service in `docker-compose.yml`, and
mentions in `CLAUDE.md`, `README.md`, `DEPLOY.md`, `docs/FULL-DESIGN.md` and `docs/KEYS.md`. `docs/FULL-DESIGN.md` says Caddy
was chosen over nginx because it **handles certificates automatically**. The reason for switching has not been recorded;
when it is, it belongs in `docs/DECISIONS.md` next to this.

### What Caddy did, and what replaces it

| Caddy did | With nginx |
|---|---|
| Got and renewed TLS certificates by itself | `certbot` with a renewal timer, for the hub. An Origin CA certificate for proxied hostnames. **A failed renewal is a silent outage in 90 days, so monitor certificate expiry** (`INTEGRATE.md` already asks for it) |
| Redirected HTTP to HTTPS | An explicit `server` block on port 80 |
| `encode zstd gzip` | `gzip on`. nginx has no zstd out of the box |
| One JSON access log | `log_format` set by hand. Ingest keys are in the URLs, so decide whether the log should keep them |
| Passed `X-Forwarded-*` for us | Set explicitly with `proxy_set_header`. The hub reads `X-Forwarded-Proto` to mark its cookie `Secure` (`hub/auth.py`), so this one must be right |
| One file for everything | A `conf.d` file per hostname |

### A starting configuration for the hub

For the hub's DNS-only hostname. **Tested:** it passes `nginx -t` on 1.27, and run in front of an echo server it replaced
a faked `X-Forwarded-For`, dropped a faked `CF-Connecting-IP`, forced `X-Forwarded-Proto` to `https`, refused a 100 KB body
with 413, and answered a flood on the browser route with 429 after the first 30. **Not tested:** certificate issue and
renewal, and a real Cloudflare in front.

```nginx
limit_req_zone $binary_remote_addr zone=browser_ingest:10m rate=30r/m;
limit_req_status 429;   # nginx answers 503 by default; the hub's own brake says 429

server {
    listen 80;
    server_name nightjar.example.co.ke;

    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl;
    http2 on;
    server_name nightjar.example.co.ke;

    ssl_certificate     /etc/letsencrypt/live/nightjar.example.co.ke/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nightjar.example.co.ke/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    gzip on;
    gzip_types application/json text/css application/javascript;

    # Reports are small. The browser route refuses anything over 16 KB itself; this stops the body being read at all.
    client_max_body_size 64k;

    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    # DNS-only: the socket address IS the caller. Never pass on a chain the caller wrote.
    proxy_set_header X-Forwarded-For   $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    # Nobody proxies this hostname, so a CF-Connecting-IP header was written by the caller.
    proxy_set_header CF-Connecting-IP  "";

    location /ingest/browser/ {
        limit_req zone=browser_ingest burst=20 nodelay;
        proxy_pass http://hub:8000;
    }
    location /ingest/ { proxy_pass http://hub:8000; }
    location = /healthz { proxy_pass http://hub:8000; }
    location / { proxy_pass http://hub:8000; }
}
```

The `limit_req` is a second brake on top of the hub's own, not a replacement for it.

### What changes in the repository, in order

1. `deploy/nginx/` replaces `deploy/Caddyfile`, with this file and one for each proxied client hostname.
2. `docker-compose.yml`: the `caddy` service becomes `nginx`, plus a way to run `certbot` (a container or a timer on the host).
3. `DEPLOY.md` (step 2's "Caddy cannot issue a certificate until it resolves" and the TLS paragraph), `CLAUDE.md`'s stack table,
   `README.md`, `docs/FULL-DESIGN.md` and `docs/KEYS.md`.
4. `docs/DECISIONS.md`: a new entry that reverses the Caddy choice, with the reason.
5. Test the whole path on a staging VPS first. A broken ingress is the one change that takes every alert path down at once.

### Open questions

1. **Why nginx?** Whatever the reason is (a client app that needs it, familiarity, a module), it decides things like
   whether client sites share this nginx or run their own.
2. **Should the console sit on the same hostname as ingest?** The sketch above does, which exposes the console login to
   the internet directly with no Cloudflare in front. A separate proxied hostname, ideally behind Cloudflare Access
   (**Unchecked:** whether that is free for one user), would give the console a layer the ingest path deliberately does
   not have.
3. **`certbot` in a container or on the host?** Host is simpler to monitor; a container is simpler to reproduce.

## Sources

Read while writing this, in October 2026:
[Bot Fight Mode](https://developers.cloudflare.com/bots/get-started/bot-fight-mode/),
[WAF custom rules](https://developers.cloudflare.com/waf/custom-rules/),
[the Skip action](https://developers.cloudflare.com/waf/custom-rules/skip/),
[SSL/TLS modes](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/),
[default cache behaviour](https://developers.cloudflare.com/cache/concepts/default-cache-behavior/),
[Always Online](https://developers.cloudflare.com/cache/how-to/always-online/),
[HTTP headers](https://developers.cloudflare.com/fundamentals/reference/http-headers/),
[connection limits](https://developers.cloudflare.com/fundamentals/reference/connection-limits/),
[Email Routing](https://developers.cloudflare.com/email-routing/).

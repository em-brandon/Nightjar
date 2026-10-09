# Wiring apps into Nightjar

Four ways something reports in. Most client sites need the first two.

| Source | Endpoint | What it catches |
|---|---|---|
| Your own apps | `POST /ingest/{key}/event` | Unhandled exceptions, with stack traces |
| Uptime checks | `POST /ingest/{key}/webhook` | Site down, certificate expiring, slow responses |
| Sentry / GlitchTip | `POST /ingest/{key}/webhook` | Richer capture — source context, releases, breadcrumbs |
| Gitea | `POST /ingest/gitea` | Failed deploys, pushes to `main`, stale PRs |

Every site gets its own `ingest_key` (in the URL) and `ingest_secret` (signs the
body). Create them with `seed.py`, or from the console once chunk 3 exists.

**The key is not a password.** It identifies which site is reporting. The HMAC
signature is what proves the report is genuine. Both are needed in production.

---

## 1. Your own apps

> **Keys, secrets and what each can do:** [`docs/KEYS.md`](docs/KEYS.md), with diagrams. For a **web page**
> (a site with no backend, or any frontend) use the browser reporter in section 1b, which holds no secret.

> **At three or four apps, stop copying this file.** Publish it as
> `@brandon/nightjar` (npm) and `nightjar-reporter` (PyPI) so a fix is one
> `update` rather than N edits, and add an onboarding CLI for the key pairs.
> See chunk 4b in `CLAUDE.md`. Below that threshold, copying is less work.

### FastAPI

Copy `client/reporter.py` into the project. Three lines:

```python
from reporter import install_reporter

install_reporter(
    app,
    url="https://nightjar.yourdomain/ingest/<site-key>/event",
    secret=os.environ["NIGHTJAR_SECRET"],
    environment="production",
)
```

That registers a global exception handler. Any unhandled error is reported and
the user still gets a clean 500 — the reporter swallows its own failures, so a
Nightjar outage can never take down a client's site.

### Django

Same idea, as middleware:

```python
# nightjar_middleware.py
import hashlib, hmac, json, os, traceback
import httpx

URL    = os.environ["NIGHTJAR_URL"]
SECRET = os.environ["NIGHTJAR_SECRET"].encode()


class NightjarMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exc):
        body = json.dumps({
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "frames": [
                {"file": f.filename, "module": f.filename.rsplit("/", 1)[-1],
                 "function": f.name, "lineno": f.lineno, "context": f.line}
                for f in traceback.extract_tb(exc.__traceback__)
            ],
            "level": "error",
            "environment": os.environ.get("ENV", "production"),
            "request": {"method": request.method, "path": request.path},
        }).encode()

        sig = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        try:
            httpx.post(URL, content=body, timeout=3.0, headers={
                "Content-Type": "application/json", "X-Hub-Signature": sig})
        except Exception:
            pass          # never let reporting break the request
        return None       # let Django's own handler continue
```

Add `"nightjar_middleware.NightjarMiddleware"` to `MIDDLEWARE`.

### Node / Express

```js
const crypto = require('crypto');

const URL    = process.env.NIGHTJAR_URL;
const SECRET = process.env.NIGHTJAR_SECRET;

function report(err, req) {
  const body = JSON.stringify({
    exception_type: err.name || 'Error',
    message: err.message || '',
    frames: (err.stack || '').split('\n').slice(1, 12).map(line => {
      const m = line.match(/at (\S+) \((.*):(\d+):\d+\)/);
      return m
        ? { function: m[1], file: m[2], module: m[2].split('/').pop(), lineno: +m[3] }
        : { function: line.trim().slice(0, 120) };
    }),
    level: 'error',
    environment: process.env.NODE_ENV || 'production',
    request: req ? { method: req.method, path: req.path } : null,
  });

  const sig = 'sha256=' + crypto.createHmac('sha256', SECRET).update(body).digest('hex');

  fetch(URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Hub-Signature': sig },
    body,
  }).catch(() => {});      // swallow — reporting must never break the app
}

// Express error handler: must be registered LAST, after all routes
app.use((err, req, res, next) => {
  report(err, req);
  res.status(500).json({ error: 'internal server error' });
});

// and the two things Express will not catch for you
process.on('uncaughtException',  err => report(err, null));
process.on('unhandledRejection', err => report(err, null));
```

### Static sites and WordPress

No exception handler to install. These are covered entirely by uptime checks
below — which is usually enough, because what breaks is the server or the
certificate, not application code you control.

---

### 1b. Web pages (browser)

A page cannot keep a secret, so it uses a separate **public key** and the bounded route
`POST /ingest/browser/{public_key}`. Copy `client/browser/nightjar.ts` into the site (it has no dependencies).

1. List the pages that may report. This makes the public key (needs the console token):

   ```bash
   curl -X PATCH https://nightjar.yourdomain/api/sites/<id> \
     -H "Authorization: Bearer $CONSOLE_TOKEN" -H "Content-Type: application/json" \
     -d '{"allowed_origins": ["https://example.co.ke", "https://www.example.co.ke"]}'
   ```

2. Start it once, as early as possible in the page:

   ```ts
   import { init, captureException } from "./nightjar";
   init({ hub: "https://nightjar.yourdomain", key: "<public_key>", release: "v1.4.2" });
   ```

3. Uncaught errors go as warnings, which reach the digest and never wake you. For the ones that matter, such as
   a failed checkout, escalate on purpose: `captureException(err, "error")`.

For TypeScript it type-checks under `--strict`. For plain JavaScript, compile it once with `tsc` or `esbuild`.
Origins are scheme and host only: no path, no wildcard, `http://` only for `localhost`.

---

## 2. Uptime checks

**Uptime Kuma**, self-hosted on the Tiny. One container:

```yaml
services:
  kuma:
    image: louislam/uptime-kuma:1
    restart: unless-stopped
    volumes: [kuma:/app/data]
    ports: ["3001:3001"]
volumes: { kuma: }
```

For each monitor, add a notification of type **Webhook** pointing at
`https://nightjar.yourdomain/ingest/<site-key>/webhook`.

Run a **second** Kuma on the VPS watching the same sites. Two probes on
different networks is what lets Nightjar tell "the client's site is down" from
"my home internet is down" — one probe cannot distinguish those, and you will
otherwise be woken by your own ISP.

Also monitor certificate expiry. A cert lapsing is the most predictable outage
there is and the most embarrassing to miss.

---

## 3. Sentry or GlitchTip

Better capture than the reporter snippet: source context, local variables,
release tagging, breadcrumbs, source maps for frontend errors.

Install the SDK as normal, then in Sentry create an **Alert Rule** → action
**Send a notification via webhook** → your `/ingest/<key>/webhook` URL.

Sentry owns capture. Nightjar owns "is this worth your attention at 2am."
Do not let Sentry send you email or push directly, or you will have two systems
deciding when to interrupt you — which is exactly the fragmentation Nightjar
exists to remove.

GlitchTip is the self-hosted alternative and is Sentry-SDK compatible, so you
can switch later without touching client code.

---

## 4. Gitea

Repo → Settings → Webhooks → Gitea, URL `https://nightjar.yourdomain/ingest/gitea`,
set a secret, and select: Push, Pull Request, Pull Request Review, Workflow Job.

Verify `X-Gitea-Signature` the same way as the ingest HMAC.

---

## Managing secrets

Never commit `NIGHTJAR_SECRET`. Options in order of preference:

1. Gitea Actions secrets, injected at deploy time
2. SOPS-encrypted file in the repo, decrypted on the server
3. `.env` on the server, `chmod 600`, never in Git

Rotating is one row: change `ingest_secret` on the site, update the app's env,
redeploy. Old signatures start failing immediately.

---

## Verifying a new site

```bash
# temporarily set VERIFY_SIGNATURES=false to test with plain curl
curl -X POST https://nightjar.yourdomain/ingest/<key>/event \
  -H 'Content-Type: application/json' \
  -d '{"exception_type":"TestError","message":"wiring check","level":"warning"}'
```

Expect `{"issue_id": ..., "state_change": "new"}`. Resolve it in the console and
send it again — you should get `"regression"`, which proves the state machine is
working end to end.

Then break something real on staging and confirm your phone buzzes. A test
event that reaches the database but never reaches your pocket is a half-wired
system.

# Keys, secrets and tokens

Nightjar has five credentials. They look similar and do very different jobs, and mixing them up is the
easiest way to leak access. This page says what each one is, who holds it, what it can do, and what happens
when it leaks.

The diagrams are Mermaid. GitHub, Gitea and most Markdown viewers draw them.

## The short version

| Credential | What it is | Secret? | Held by | Can do | Where it lives |
|---|---|---|---|---|---|
| **Ingest key** | An address: which site is reporting. It is in the URL, `/ingest/<key>/event` | **No.** It also appears in access logs | The site's server, CI, reporters | Nothing on its own once signatures are on | Environment variable |
| **Ingest secret** (the private key) | Signs each request body with HMAC-SHA256. It is never sent | **Yes** | The site's server, CI, reporters | Report errors, deploys and stack state **as that one site** | Gitea Actions secret, then the server's `.env` |
| **Browser public key** | A separate address for web pages. It can only report errors, only from listed origins | **No, by design.** It is in the page source | A web page | Report errors for one site, within strict limits | The page's build config |
| **Master key** (`SECRET_MASTER_KEY`) | The one key every site's ingest secret is worked out from (below). Not in the database | **Yes, the most important one** | The hub only | Work out any site's ingest secret, so forge reports as every site | The VPS `secrets/secret_master_key` file, and a copy in your password manager |
| **Console token** (`CONSOLE_TOKEN`) | Signs you in to the console and `/api` | **Yes** | You only | Everything: read, resolve, mute, create sites | The VPS `.env`, your password manager |
| **Presence token** (`PRESENCE_TOKEN`) | Lets the NFC tap set home or away | **Yes** | Your iPhone Shortcut | Set presence, nothing else | The VPS `.env`, the Shortcut |

Two rules sit above the table:

1. **The console token never goes in a client's CI, a client's server or a web page.** CI holds ingest
   credentials only. A site's ingest credentials cannot read the console or mute an issue, because `/api`
   needs the console token.
2. **The ingest secret never goes in a web page.** A page's source is public. That is why browsers get their own,
   weaker key.

## Who holds what

```mermaid
flowchart LR
    subgraph hub["Nightjar hub on the VPS"]
        DB[("Postgres<br/>sites: ingest_key, ingest_secret,<br/>public_key, allowed_origins")]
        API["/api, console token"]
        ING["/ingest/key/..., signed"]
        BRW["/ingest/browser/public_key, bounded"]
    end

    subgraph client["A client's site"]
        SRV["Server code or CI<br/>holds ingest key + secret"]
        PAGE["Web page<br/>holds public key only"]
    end

    ME["You<br/>hold the console token"]

    SRV -- "signed with the secret" --> ING
    PAGE -- "no secret, Origin checked" --> BRW
    ME -- "bearer token or session cookie" --> API
    ING --> DB
    BRW --> DB
    API --> DB
```

## Signed ingest: servers, CI and reporters

Everything that runs on a machine you or the client control uses the **ingest key** and the **ingest secret**.
That includes the server reporters in `client/` (Python, Node, PHP and Java), which read `NIGHTJAR_URL` (the hub),
`NIGHTJAR_KEY` and `NIGHTJAR_SECRET`. See [`REPORTERS.md`](REPORTERS.md).

```mermaid
sequenceDiagram
    participant App as Site server or CI
    participant Caddy
    participant Hub
    participant DB as Postgres

    Note over App: body = the JSON bytes<br/>ts = the time now, in seconds<br/>sig = HMAC-SHA256(secret, ts.body)
    App->>Caddy: POST /ingest/KEY/event<br/>X-Hub-Timestamp: ts<br/>X-Hub-Signature: sha256=sig
    Caddy->>Hub: forwards, TLS ended here
    Hub->>DB: find the site with ingest_key = KEY
    alt no such site
        Hub-->>App: 404 unknown site
    else VERIFY_SIGNATURES is on
        Hub->>Hub: recompute HMAC with the stored secret<br/>compare in constant time
        alt signature missing or wrong
            Hub-->>App: 401
        else signature matches
            Hub->>DB: fingerprint, group, store
            Hub-->>App: 200 with issue_id and state_change
        end
    end
```

How the signature is made. The HMAC is over the **timestamp, a dot and the raw request bytes**, so sign exactly what you
send. The timestamp is whole seconds since 1970, and it goes in the signature so a captured request expires:

```python
ts = str(int(time.time()))
sig = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
# send them as the headers   X-Hub-Timestamp: <ts>   and   X-Hub-Signature: <sig>
```

The hub refuses a timestamp more than five minutes from its own clock (`SIGNATURE_MAX_AGE_S`), in either direction, so a
machine with a wrong clock is refused with `stale timestamp`: keep NTP on. A request with **no** timestamp is still accepted
(signed over the body alone, the original scheme) until the hub is told to require one with `REQUIRE_SIGNED_TIMESTAMP=true`.

```bash
# the same thing in a shell, which is what CI uses
TS="$(date +%s)"
SIG="sha256=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$NIGHTJAR_SECRET" -hex | sed 's/^.* //')"
curl -sS -X POST "$NIGHTJAR_URL/ingest/$NIGHTJAR_KEY/deploy" \
  -H "Content-Type: application/json" -H "X-Hub-Timestamp: $TS" -H "X-Hub-Signature: $SIG" -d "$BODY"
```

Why both a key and a secret? The key says **which site** and lets the hub find the right secret. The secret
proves the sender really is that site. Anyone can learn a key, since it sits in URLs and logs, and that is
fine, because a request with the right key and no valid signature is refused.

**`VERIFY_SIGNATURES=false` (local development) turns the check off.** Then the key alone is enough to post
anything. It must be `true` in production, and the hub refuses to start in that state without a console token.

## Browser reporting: web pages

A page cannot keep a secret. So browsers use a different route with a different key, and the hub **bounds what
the key can do** instead of trusting who holds it.

```mermaid
sequenceDiagram
    participant V as Visitor's browser
    participant Hub
    participant DB as Postgres

    Note over V: window.onerror fires<br/>the reporter builds a small JSON report
    V->>Hub: POST /ingest/browser/PUBLIC_KEY<br/>Origin: https://birdboxsystems.co.ke<br/>Content-Type: text/plain (no preflight)
    Hub->>DB: find the site with public_key = PUBLIC_KEY
    alt unknown key, or turned off
        Hub-->>V: 404
    else Origin not on the site's list, or missing
        Hub-->>V: 403
    else over a rate limit
        Hub-->>V: 429
    else accepted
        Hub->>Hub: clamp level, cap sizes, strip the page's query string
        Hub->>DB: fingerprint, group, store
        Hub-->>V: 202, with no detail
    end
```

What the hub checks, in order, and what each check is for:

```mermaid
flowchart TD
    A["Report arrives"] --> B{"Body over 16 KB?"}
    B -- yes --> X1["413"]
    B -- no --> C{"Public key known?"}
    C -- no --> X2["404"]
    C -- yes --> D{"Origin header on the<br/>site's allowed list?"}
    D -- no --> X3["403"]
    D -- yes --> E{"Under the rate limits?<br/>per key and per visitor"}
    E -- no --> X4["429"]
    E -- yes --> F{"Valid JSON in the<br/>allowed shape?"}
    F -- no --> X5["422"]
    F -- yes --> G["Clamp: never fatal, so never P1<br/>Cap every string<br/>Drop the query string and extras"]
    G --> H{"Is this fingerprint<br/>already known?"}
    H -- yes --> I["Count it"]
    H -- no --> J{"Site opened fewer than 10<br/>new issues from browsers this hour?"}
    J -- yes --> K["Open a new issue"]
    J -- no --> L["Dropped quietly, still 202"]
```

| Check | Stops | Does **not** stop |
|---|---|---|
| Origin must be on the list | Another website using a key copied from your page | Someone with `curl`, who can send any `Origin` |
| Per-key and per-visitor rate limit | A script hammering the route | A patient attacker using many addresses |
| Level clamped to `warning` or `error` | A page asking for a P1 wake-up | Nothing; this one is absolute |
| Cap on **new** issues per site per hour | A flood of *different* junk errors, which fingerprinting cannot merge | Genuine bugs beyond the cap are dropped until the hour rolls over |
| Body and field size caps | Storage abuse | Nothing |

The Origin check is a filter, not authentication. The limits are what actually protect you.

### Turning it on for a site

```bash
# 1. list the pages allowed to report. This makes the public key. Use your console token.
curl -X PATCH https://nightjar.example/api/sites/9 \
  -H "Authorization: Bearer $CONSOLE_TOKEN" -H "Content-Type: application/json" \
  -d '{"allowed_origins": ["https://birdboxsystems.co.ke", "https://www.birdboxsystems.co.ke"]}'
# the reply includes "public_key": "..."
```

```ts
// 2. in the site's code, with the file client/browser/nightjar.ts copied in
import { init } from "./nightjar";
init({ hub: "https://nightjar.example", key: "<public_key>", release: "v1.4.2" });
```

Rules for origins: scheme and host only, such as `https://example.com` or `https://example.com:8443`. No path, no
wildcard, no `http://` except `localhost` for development. List `www` and the bare domain separately.

## Console token and sessions

```mermaid
sequenceDiagram
    participant You
    participant Hub

    You->>Hub: POST /api/auth/login with the token
    alt wrong token
        Hub-->>You: 401, and after 10 failures a minute even the right one gets 429
    else right token
        Hub-->>You: HttpOnly cookie holding HMAC(token, "session"), not the token
        You->>Hub: GET /api/... with the cookie
        Hub-->>You: 200
    end
    Note over Hub: Changing CONSOLE_TOKEN signs everyone out,<br/>because the cookie value is derived from it
```

Scripts can skip the cookie and send `Authorization: Bearer <token>`. The cookie is only marked `Secure` over
https, so use the console behind Caddy's TLS.

## Life of a site's credentials

```mermaid
flowchart TD
    A["POST /api/sites<br/>console token"] --> B["Hub makes ingest_key and ingest_secret<br/>and returns them once"]
    B --> C[".site-keys on your machine<br/>(git-ignored)"]
    C --> D["Copy into Gitea Actions secrets<br/>NIGHTJAR_KEY, NIGHTJAR_SECRET"]
    D --> E["CI writes them to the server's .env at deploy"]
    E --> F["App or reporter signs requests"]
    C --> G["Delete .site-keys once stored"]
    A --> H["PATCH allowed_origins<br/>makes the public key"]
    H --> I["Public key goes in the site's build config"]
```

`POST /api/sites` is the only time the secret is returned. The ingest key and the public key are shown again
whenever a site is edited, since neither is secret.

## Derived ingest secrets

The hub does not store a new site's ingest secret. It works it out: `HMAC-SHA256(master key, "nightjar-site-secret-v1|<site id>|<version>")`,
as 43 URL-safe characters. The app still holds an ordinary secret string and signs exactly as before, so no reporter changed. A copy of the
database alone now holds nothing that can forge a report.

- **Existing sites keep their stored secret** (`secret_version` 0) and keep verifying. The first **Rotate secret** moves a site to a derived
  one (version 1) and blanks the stored value; each rotation after that adds one to the version.
- **With no master key** (local development) new sites still get a stored secret. **Production refuses to start without one**, and a hub
  that has derived sites refuses to start if the key has gone missing.
- **The master key is now the thing to protect.** Leaked, it exposes every site at once; lost, every derived site needs a new secret
  (rotate each one with a new key set, and give each app its new value). It lives in `secrets/secret_master_key`, so keep a copy in your
  password manager apart from the database backups. A backup of the database plus the key is a full restore; either alone is not.
- It protects the database, not the machine: anyone who can read the hub's secrets directory has the key.

## If one leaks

| Leaked | What an attacker can do | What to do |
|---|---|---|
| **Ingest key only** | Nothing useful once signatures are on. It is an address | Nothing |
| **Master key** | Forge reports as **every** derived site | Set a new `SECRET_MASTER_KEY`, restart, then rotate every site and give each app its new secret. Until a site is rotated its reports are refused |
| **Ingest secret** | Post fake errors, fake deploys and fake stack reports **as that one site**. They cannot read anything, mute anything or touch another site | Rotate it (below), update the app's environment, redeploy |
| **Browser public key** | Post error reports from `curl`, within the rate limits and new-issue cap. Cannot read, cannot reach another route, cannot be P1 | Empty `allowed_origins` to revoke it, then set them again for a new key |
| **Console token** | Everything, including creating sites and muting issues | Change `CONSOLE_TOKEN` in the VPS `.env` and restart. Every session ends at once |
| **Presence token** | Set home or away. This can only change which channels fire, never whether something alerts | Change `PRESENCE_TOKEN` and update the Shortcut |

**Rotating an ingest secret.** Use **Rotate secret** on the site's row in the console's Sites tab (or
`POST /api/sites/{id}/rotate-secret`). The new secret is shown once, and the old signatures stop working immediately, so have
the app's settings open before you press it. If the console is not available, **and the site still has a stored secret** (its
`secret_version` is 0), it is one SQL statement. A derived site cannot be rotated this way, because its secret is not
in the database; use the console or the API:

```bash
NEW=$(python -c "import secrets; print(secrets.token_urlsafe(24))")
docker compose exec db psql -U nightjar -d nightjar \
  -c "UPDATE sites SET ingest_secret = '$NEW' WHERE name = 'bird-box-systems';"
echo "$NEW"   # put this in the Gitea secret, then redeploy
```

Rotating the ingest **key** is the same statement on `ingest_key`, and also changes the URL the app posts to.

## Leak runbook

Read this before you need it. When something leaks you will be tired and in a hurry, and the order matters more than the speed.

**The order, for every credential: revoke, rotate, delete, record.**

1. **Revoke.** Make the exposed value stop working. Do this first, before you work out how it got out. Assume it was read.
2. **Rotate.** Make a new one and put it where the app expects it, so the app is not left broken.
3. **Delete.** Remove the old value from wherever it was exposed (a commit, a log, a chat, a screenshot, a ticket). This is tidying, not
   protection: a value that was public for a minute is compromised for good, which is why revoking comes first. Rewriting git history does
   not un-leak anything that was already pushed or cloned.
4. **Record.** Write down what leaked, when, how you found out, what it could do, and what you did. For a site's secret the hub already
   records the rotation (the site's **History** button); the rest is yours to write, in whatever notes you keep, within the day.

Rotating **needs the console** (or the API with the console token). If the console token itself leaked, do that one first.

### Ingest secret (one site)

Reach: forge errors, deploys and stack state as that one site. Nothing else.

1. **Revoke and rotate in one step:** Sites tab, that site, **Rotate secret**. The old secret stops working at once. (API:
   `POST /api/sites/{id}/rotate-secret` with the console token.) The new secret is shown once, so have the next step open before you press it.
2. Put the new value in the Gitea Actions secret `NIGHTJAR_SECRET`, then redeploy the app (or edit its `.env` and restart it). Until you do,
   that site's reports are refused with 401, which is the intended failure: it is loud on the hub's side, not silent.
3. **Check:** send a test event from the Sites tab (**Test**), and send one signed with the *old* secret and confirm it gets `401`.
   `./test_ingest.sh <ingest_key>` with `NIGHTJAR_SECRET` set to each value does both.
4. **Delete** the old value from CI, shell history, `.site-keys` and wherever it was seen.
5. **Record.** The History button shows `secret_rotated, derived, version N`.

If you cannot reach the console, a site that still has a stored secret (`secret_version` 0) can be rotated with the SQL above. A derived
site cannot, because its secret is not in the database.

### Browser public key (one site)

Reach: post error reports from `curl` within the rate limits and new-issue cap. It cannot be P1 and cannot read anything.

1. **Revoke:** Sites tab, **Edit**, empty the allowed pages, **Save**. The key stops working immediately.
2. **Rotate:** add the pages back and **Save**. This makes a new key. Put it in the site's build config and redeploy.
3. **Check:** a report to the old key returns 404.
4. **Delete / record:** nothing secret was exposed, so there is little to delete. The History button shows `browser_key_revoked` and `browser_key_issued`.

### Console token

Reach: everything, including creating sites, rotating secrets and muting issues. This is the worst one short of the master key.

1. **Revoke and rotate:** on the VPS write a new value into `secrets/console_token` (`openssl rand -base64 32 | tr -d '\n' > secrets/console_token`),
   then `docker compose restart hub`. Every session ends at once, because the cookie is derived from the token.
2. Sign in with the new token and put it in your password manager.
3. **Check:** the old token returns 401 (`curl -H "Authorization: Bearer <old>" https://<hub>/api/sites`).
4. **Assume the attacker used it.** Open each site's **History** and look for changes you did not make (a new URL, new browser pages, a rotation).
   If any site's secret was displayed or rotated while the token was exposed, rotate that site again.
5. **Delete / record** as above. There is no audit line for a token change; write it down.

### Presence token

Reach: set home or away. That changes which channels fire, never whether something alerts.

1. **Revoke and rotate:** write a new value into `secrets/presence_token`, `docker compose restart hub`.
2. Update the iOS Shortcut that taps the NFC tag, and tap once.
3. **Check:** the old value returns 401 from `POST /presence`, the new one 200. Confirm a push still arrives with presence away: it must.
4. **Delete / record** as above.

### Master key (`SECRET_MASTER_KEY`)

Reach: work out the secret of every derived site, so forge reports as all of them. Rare, and the most work.

1. **Revoke:** put a new key in `secrets/secret_master_key` (`openssl rand -base64 32 | tr -d '\n'`) and `docker compose restart hub`. Every derived
   site's reports are now refused with 401, because their secrets came from the old key. That is the revoke.
2. **Rotate every site:** Rotate secret on each one in the Sites tab, update each app, redeploy. A site stays refused until you do.
   A site you have not got to yet is down in the sense of *not reporting*; the Sites tab shows it as never or long since reported.
3. **Check:** one site at a time, as for an ingest secret.
4. Put the new key in your password manager and **delete** the old one. **Record** the date, because every backup of the database
   restores with the key that was current when you restore it, so keep the old key alongside any backup older than this.

### Not covered here

The Pushover keys, the Postgres password and the `age` backup key are not in this runbook yet. The backup key's loss is in `DEPLOY.md`.

### The drill

A runbook nobody has tried is a hypothesis. The steps for an ingest secret are exercised by the tests (`tests/test_derived_secrets.py`: rotating
refuses the old secret and accepts the new one), but that is not you doing it under stress with a real app.

**Status: rehearsed, not drilled.** The real drill needs a deployed hub and a real app, and neither exists yet. Reading the runbook through did find one fault,
now fixed: the two `tr -d '\n'` commands (console token and master key) had lost their backslash, so the command was split over two
lines and, copied from the rendered page, would have left a newline at the end of the secret file.

**Rehearsal, 3 November 2026.** Run by Claude on a development machine, against a throwaway hub and database (signatures and the
console token on, a master key set), not a deployment and not a person under stress. It proves the commands and the failure modes, not the
timing. Everything below behaved as this page says:

| Step | Result |
|---|---|
| Signed event with the current secret | accepted |
| Rotate secret (`POST /api/sites/{id}/rotate-secret`) | 0.3 s; the new secret differs from the old |
| Sites tab **Test** (`test-event`) | 200 |
| Event signed with the new secret | accepted |
| Event signed with the **old** secret | `401`, `{"detail":"bad signature"}` |
| An app still holding the old secret | its reporter logged `the hub answered 401 to a report ... Check the site's key and secret.`; with the new secret it logged nothing |
| History | `secret_rotated, derived, version 2` |
| Console token changed, hub restarted (4 s) | old bearer token `401`, new `200`, and a session cookie made with the old token `401` |
| Presence token changed, hub restarted | old `401`, new `200` |
| A rotated secret across that restart | still accepted (derived from the master key, not lost) |

Two things it showed: `./test_ingest.sh` prints `bad signature` for a refused secret but not the status, and its hint ("re-run with
`NIGHTJAR_SECRET` set") is wrong when you are deliberately using the old secret, so read that line as the refusal you wanted. And it was
not run for the master key, the Pushover keys, the Postgres password or the backup key.

**When the hub is deployed**, do this once, with a stopwatch, on one real site that is not a client's production site, and fill in the
table. Anything in the "unclear" column is a fix to this page, so change the page in the same sitting.

| Step | Command or place | Seconds | Unclear or wrong? |
|---|---|---|---|
| 1. Have the app's settings open | Gitea `NIGHTJAR_SECRET`, or the app's `.env` | | |
| 2. Rotate | Sites tab, that site, **Rotate secret** | | |
| 3. Put the new value in and redeploy | Gitea secret, then the app's deploy | | |
| 4. Test event with the new secret | Sites tab, **Test**, expect it accepted | | |
| 5. Send with the old secret | `NIGHTJAR_SECRET=<old> ./test_ingest.sh <ingest_key>`, expect `bad signature` (a 401) | | |
| 6. The app's own reporter says so | While the app still has the old secret, its log should show `the hub answered 401 to a report ... Check the site's key and secret.` | | |
| 7. Read the History button | expect `secret_rotated, derived, version N` | | |

Then repeat steps 1 to 3 of the **console token** and **presence token** runbooks the same way (rotate, restart, old value returns 401),
and write the total time at the bottom. Skip the master key unless you are prepared to rotate every site.

Step 6 is new: since the reporters now say so in the app's log when the hub refuses a report, a rotation that was not carried through
to the app is visible there instead of looking like an app with no errors.

## Where each one is kept

| Place | Holds | Rules |
|---|---|---|
| `.site-keys` (repo root) | Every site's key and secret, written when sites were created | Git-ignored. Move the values into Gitea secrets, then delete the file |
| **Gitea Actions secrets** | Each site's `NIGHTJAR_KEY` and `NIGHTJAR_SECRET` | The preferred home: rotation is one edit, not a hunt across servers |
| The server's `.env` (`chmod 600`) | The same, written by CI at deploy | Never committed |
| The VPS `secrets/` directory | `secret_master_key`, `console_token` and the rest | Never committed. Copy `secret_master_key` to your password manager |
| The VPS `.env` | `CONSOLE_TOKEN`, `PRESENCE_TOKEN`, `POSTGRES_PASSWORD` | Never committed. `CONSOLE_TOKEN` needs 16 characters or more |
| A page's build config | The browser public key | Safe to commit. It is public by design |
| Your password manager | The console token | The only copy outside the VPS |

## Limits worth knowing

- **A captured request can still be replayed for five minutes.** Reporters sign a timestamp and the hub refuses one older
  than `SIGNATURE_MAX_AGE_S` (300), so a capture expires, and a fresh timestamp cannot be attached to an old capture. Inside
  the window a replay is accepted and recorded again; the damage is a duplicate row, not access. Until
  `REQUIRE_SIGNED_TIMESTAMP=true`, a request sent **without** a timestamp is still accepted, so an old capture of one is
  replayable for ever: turn it on once every reporter has been updated.
- **Keys appear in Caddy's access log**, because they are in the URL. That is acceptable for an identifier that is
  not secret, which is why signatures matter. Never put a secret in a URL.
- **The browser limits are in memory.** A hub restart resets them. They are a brake, not an accounting system.
- **Behind Cloudflare or Caddy the hub reads the visitor's address from a header** (`CF-Connecting-IP`, then
  `X-Forwarded-For`). A caller can fake it to dodge the per-visitor limit, but the per-key limit and the new-issue
  cap still hold.
- **A Cloudflare Worker is the upgrade path** if browser abuse ever becomes real: the Worker holds a secret,
  signs on the page's behalf and adds its own checks, so the page never talks to the hub directly. It is not
  built because the bounds above are enough for now and a Worker is another thing to maintain.

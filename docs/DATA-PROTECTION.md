so# Data protection (planned)

> **Status: planned, not started.** This records what is and is not encrypted today, and the eight changes that are
> planned: two small ones, encrypted backups, derived secrets, a secret scanner, secrets as mounted files, an audit trail and a leak
> runbook. The rest is listed so it is not forgotten, and is not committed to.

## Where things stand

| | Today |
|---|---|
| **A report on the wire** | Encrypted by TLS **only if the hub URL is `https://`**. Caddy provides the certificate in production |
| **Who sent it, and was it altered** | Proven by the HMAC signature with the site's ingest secret. This is authenticity, not secrecy |
| **The reporters and `http://`** | They accept it. A mistyped URL sends readable JSON, though never the secret, which only signs |
| **The browser** | The page's address is sent without its query string. The key is public and signs nothing |
| **In the database** | Error messages, frames and request paths are stored as readable text. Ingest secrets are readable too, because the hub needs them to check each signature |
| **Backups** | `deploy/backup.sh` writes a gzip, which is compression, not encryption. The optional `rclone` copy is not encrypted by the script |
| **The disk** | Nothing in the repository turns encryption on. That depends on the VPS provider |

## Planned work

### 1. The reporters refuse a plain `http://` hub

**Problem.** A typo or a copied development URL makes a production reporter send error text unencrypted, with no warning.

**Change.** Each reporter (Python, Node, PHP, Java and browser) treats an `http://` hub as unusable unless the host is
loopback (`localhost`, `127.0.0.1` or `::1`), which development needs.

**One rule that must not be broken.** Refusing must **not** be silent. A reporter that quietly does nothing is the failure
this project is built to avoid, because a mistyped URL would look like an app with no errors. So it sends nothing, **never
throws**, and writes one clear line to the app's own log (standard error, `error_log`, or the console) the first time. That
keeps the rule that a reporter never breaks the app.

**Done when**
- each reporter has a test that an `http://` remote host sends nothing, throws nothing and logs once, and that `https://`
  and loopback `http://` still work;
- `tests/test_server_reporters.py` still passes for all five runtimes (its hub is `http://hub.test`, so the test setup
  needs a loopback or `https` address);
- `docs/REPORTERS.md` says it.

### 2. HSTS on the hub's Caddyfile

**Problem.** A browser that has been to the console can still be tricked into using plain HTTP on the first request, which
exposes the console login.

**Change.** Add `header Strict-Transport-Security "max-age=..."` to the hub's site block in `deploy/Caddyfile`.

**Care needed.**
- **HSTS is hard to undo.** Start with a short `max-age` (a day), confirm nothing breaks, and then raise it.
- **No `includeSubDomains` or `preload` until every subdomain of the hub's domain serves HTTPS.** The hub should be on a
  domain of its own (`docs/CLOUDFLARE.md`), which makes this safer.
- **The hub's hostname only.** Client sites behind Cloudflare get their HSTS setting in Cloudflare, which is a separate
  switch in each zone.

**Done when:** `curl -I` against the hub shows the header, and the change was tried on Caddy in a throwaway container the way
the other Caddyfile additions in `docs/CLOUDFLARE.md` were.

### 3. Encrypt backups before they leave the machine

**Problem.** `deploy/backup.sh` writes a gzip of the whole database, which holds every site's ingest secret and all the
error text. The optional `rclone` copy goes to the remote readable unless that remote encrypts. A backup is the likeliest
place for a copy to escape, so this comes before the other secret-protection work.

**Change.** Encrypt the dump before it is written or copied, and choose one tool:
- **`age` with a public key.** The server holds only the public key, so a stolen server cannot read its own backups. It
  adds one small tool.
- **`rclone crypt`.** It wraps the existing `RCLONE_REMOTE`, so the off-site copy is encrypted with no new tool, but the
  local copy under `/var/backups` would still be readable unless it is encrypted too.

**Care needed.**
- **Key custody.** The key that decrypts must live somewhere other than the VPS, such as a password manager. Lose it and the
  backups are useless, and the failure only shows up on the day you need them.
- The script already says it: *a backup you have never restored is a hypothesis*. Encrypting adds a way to fail, so the test is
  a real restore from an encrypted file onto a clean database.

**Done when:** the script writes encrypted files, a restore from one has been done and checked, and `DEPLOY.md` says how
to hold the key.

### 4. Derive each site's secret instead of storing it (before other people's secrets are stored)

**Problem.** The hub keeps every ingest secret readable in the database, because verifying a signature needs the secret
itself (a hash is no use, unlike a password). A leaked database therefore lets an attacker forge reports as every site. See
"Where things stand".

**Change.** The hub keeps **one master key in its environment, never in the database**, and works out each site's secret
from it: roughly `HMAC-SHA256(master, "nightjar-site-secret-v1|" + site_id + "|" + version)`. The apps keep a secret string
exactly as now, so **no reporter changes**. A database leak alone then exposes nothing.

**What it needs.**
- a `SECRET_MASTER_KEY` setting that production refuses to start without, as it does for `CONSOLE_TOKEN`;
- a `secret_version` column (a migration), where rotating a site's secret is bumping its version;
- existing sites keep their stored secret and keep verifying until they are rotated, so nothing breaks on the day;
- the rotate endpoint already planned in `docs/ONBOARDING-DASHBOARD.md` (`POST /api/sites/{id}/rotate-secret`) is the natural
  place for the version bump, so the two are built together.

**Trade-offs.**
- **The master key becomes the thing to protect.** Leaked, it exposes every site at once; lost, every app has to be given a
  new secret. It must be backed up separately from the database.
- It protects the database, not the machine: an attacker who can read the hub's environment has the master key.

**Not urgent for one operator.** It matters when other people's secrets are held (decision #54, step 3). Do it before then, and
with the rotate endpoint.

**Done when:** tests show the derivation is stable, differs per site and per version, that verification works for both
derived and legacy sites, and that rotating invalidates the old secret; `docs/KEYS.md` is updated; and a production hub
refuses to start without the master key.

### 5. A secret scanner before every commit

**Problem.** Nothing stops a real secret being committed. `.env` and `.site-keys` are git-ignored, and the history was checked
by hand before the last push, but that depends on remembering. OWASP's secrets guidance asks for detection at the developer's
machine, with more than one tool where possible.

**Change.** A pre-commit hook that scans the staged changes, and the same scan in the Gitea pipeline so a commit made without the
hook is still caught. `detect-secrets` and `gitleaks` are the usual choices and OWASP names `detect-secrets` as mature; pick one
and check its current setup before relying on it.

**Care needed.** The repository has harmless look-alikes: the `hub:hub` password in `.env.example`, test keys such as `s3cret`,
and long example tokens in the docs. A baseline or allow list is needed or the hook gets switched off, which is worse than none.

**Done when:** a deliberately fake secret in a throwaway commit is blocked, the existing history scans clean, and the allow list
is short and commented.

### 6. Production secrets as mounted files, not environment variables

**Problem.** `docker-compose.yml` takes `POSTGRES_PASSWORD` and `CONSOLE_TOKEN` from `.env` as environment variables. OWASP rates
environment variables as a last resort: they show up in `docker inspect`, in process listings and in crash dumps.

**Change.** Compose `secrets:` that mount each value as a file with strict permissions under `/run/secrets`. The hub reads them
through `pydantic-settings`' `secrets_dir`, and the official Postgres image already reads `POSTGRES_PASSWORD_FILE`. Local development
keeps using `.env`.

**Care needed.**
- `DATABASE_URL` has the password inside it, so production must build it from the parts or read it from a file.
- The hub already refuses to start in production without a console token. That check must still hold when the token comes from a file.

**Done when:** a production-shaped Compose file runs with no secret in `.env` and none visible in `docker inspect`, the hub still
refuses to start without them, and `DEPLOY.md` says how to create the files.

### 7. An audit trail for sites and secrets

**Problem.** Issue actions are logged ("who muted that, and why is it quiet?"). Creating a site, changing its URL or browser origins,
and above all rotating a secret leave no record, so "who changed that and when?" has no answer.

**Change.** An append-only `audit_log` table (what, which site, when, and who, which is just "the console" while there is one
token) for site creation, URL and origin changes, secret rotation and revocation. **It never stores a secret value.**

**Care needed.** OWASP asks that audit logs resist tampering. The hub offers no way to edit or delete rows, and a copy should
leave the machine with the backups. Trustworthy timestamps come from the server clock, so the VPS must keep time in sync.

**Done when:** each of those actions writes a row, a test proves no row ever contains a secret, and the console can show the
trail for a site.

### 8. A runbook for a leaked secret

**Problem.** `docs/KEYS.md` says what each leak costs and gives one SQL statement for rotation, but there is no single set of
steps to follow under stress, and none has been practised.

**Change.** A short runbook in `KEYS.md`, in the order OWASP gives: **revoke** the exposed secret, **rotate** it and update the app,
**delete** it from wherever it was exposed (a commit, a log, a chat, a screenshot), then **record** what happened. It is written
for each credential: ingest secret, browser key, console token, presence token.

**Done when:** the steps are written, and have been tried once for real by rotating one site's secret with the apps updated and the
old secret refused. Doing this together with the rotate endpoint (items 4 and the onboarding plan) saves a second round of work.

## Considered, not planned

These came up in the same conversation. None is decided.

- **Disk encryption** for the database volume, or a VPS that offers it.
- **A scrubbing option** for personal data in error messages (emails, ID numbers). Error text is stored as the app wrote it.
  Worth doing before other people's data is stored (decision #54).
- **Asymmetric signing** (a private key in the app, only a public key in the hub) instead of a shared secret. See below.

### Asymmetric signing, in brief

Today the app and the hub share one secret, and the hub stores it readable so it can check each signature. (Planned work 4 above reduces that risk without changing the reporters.) With a key pair
the app signs with a **private** key that never leaves it, and the hub keeps only the **public** key, which can verify but
cannot forge.

**What it would buy.** A leaked database no longer lets anyone forge reports for every site. That matters little with one
operator and a lot with many customers (decision #54), where the hub would otherwise hold N readable secrets.

**Why it is not done.** Mostly cost, and it is a fair choice to revisit:

| | Shared secret (HMAC, today) | Key pair (for example Ed25519) |
|---|---|---|
| Every reporter's standard library has it | Yes: Python, Node, PHP, Java and the `openssl` command | No |
| Python (`reporter.py`) | `hmac` and `hashlib` | The standard library has no Ed25519, so it needs the third-party `cryptography` package, breaking the no-dependency rule |
| Java 11 (our floor) | `javax.crypto.Mac` | Ed25519 arrived in Java 15, so Java 11 would need ECDSA instead and its different signature format |
| PHP, Node | built in | Node's `crypto` has it. PHP has it through `libsodium`, which I believe ships with PHP 7.2 and newer but a host can disable it (**unchecked**) |
| Shell in CI | `openssl dgst -hmac` | Needs a new enough `openssl` (**unchecked**) |
| Onboarding | The hub makes the secret and shows it once | The app should make the pair and register the public key, a bigger change to the flow |
| A web page | Cannot hold either | Cannot hold either, so browsers do not benefit |

**What it does not fix.** It does not hide the body (TLS does that) and it does not stop a captured request being replayed
(a timestamp in the signed body does that, with either scheme).

**A middle path** if the hub ever holds many customers' secrets: keep HMAC, but store each secret encrypted with a key held
outside the database, so a database leak alone is not enough.

## Source

The secret-handling items follow the [OWASP Secrets Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html),
read in October 2026: central and least-privilege handling, regular and automated rotation, keys not stored beside the data they
protect, environment variables as a last resort, CI treated as production, secrets never logged, tamper-resistant audit logs,
leak detection early, and revoke then rotate then delete then record when one leaks.

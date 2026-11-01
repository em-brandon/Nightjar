# Data protection (planned)

> **Status: in progress.** This records what is and is not encrypted today, and the eight changes planned: two small ones,
> encrypted backups, derived secrets, a secret scanner, secrets as mounted files, an audit trail and a leak runbook. **Built so far:**
> 1 (the reporters refuse a plain `http://` hub), 2 (HSTS), 3 (encrypted backups), 4 (derived secrets), 5 (secret scanner), 6 (secrets as mounted files) and 7 (audit trail). Only 8 (the leak runbook) is not started. The "Considered, not planned" list is not
> committed to.

## Where things stand

| | Today |
|---|---|
| **A report on the wire** | Encrypted by TLS **only if the hub URL is `https://`**. Caddy provides the certificate in production |
| **Who sent it, and was it altered** | Proven by the HMAC signature with the site's ingest secret. This is authenticity, not secrecy |
| **The reporters and `http://`** | They refuse it, out loud, except for loopback. A mistyped URL used to send readable JSON, though never the secret, which only signs |
| **The browser** | The page's address is sent without its query string. The key is public and signs nothing |
| **In the database** | Error messages, frames and request paths are stored as readable text. A site made or rotated since item 4 has no secret stored: it is derived from `SECRET_MASTER_KEY`. Older sites keep a readable stored secret until they are rotated |
| **Backups** | `deploy/backup.sh` encrypts the dump with `age` to a public key before it is written, so the server cannot read its own backups. The key that decrypts is kept off the VPS |
| **The disk** | Nothing in the repository turns encryption on. That depends on the VPS provider |

## Planned work

### 1. The reporters refuse a plain `http://` hub (built)

**Problem.** A typo or a copied development URL makes a production reporter send error text unencrypted, with no warning.

**Change.** Each reporter (Python, Node, PHP, Java and browser, and the four home-side scripts: stack, home, Jellyfin and
thermal) treats an `http://` hub as unusable unless the host is
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

### 2. HSTS on the hub's Caddyfile (built, at one day)

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

### 3. Encrypt backups before they leave the machine (built, restore tested on a stand-in dump)

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

**Built with `age`.** `deploy/backup.sh` refuses to run without `BACKUP_AGE_RECIPIENT`, writes through a temp name so a failed
run leaves no truncated file, and prunes old plain `.sql.gz` files too. `deploy/restore.sh` decrypts and fails loudly on a wrong key.
Tested in an Alpine container: ciphertext contains no plaintext, the right key restores, a wrong key exits non-zero. **Not yet done:**
a restore of a real `pg_dump` onto a clean Postgres. Do that once on the VPS before relying on it.

**Done when:** the script writes encrypted files, a restore from one has been done and checked, and `DEPLOY.md` says how
to hold the key.

### 4. Derive each site's secret instead of storing it (built)

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

**Built.** `hub/derive.py` (`derive`, `site_secret`, `check_database`), `SECRET_MASTER_KEY` (32+ characters; production refuses to start
without it, in `auth.check_config`), and `sites.secret_version` (migration 0013; 0 = the stored secret is the secret). Every ingest route
checks signatures through `site_secret(site)`. Creating a site, and **Rotate secret**, give a derived secret whenever a master key is set;
rotating a legacy site is how it is moved over, and it blanks the stored value. A hub with derived sites and no key refuses to start, and
a derived site whose key goes missing at runtime answers 503, never a signature checked against an empty secret. `deploy/make-secrets.sh`
generates `secrets/secret_master_key` and Compose mounts it. The audit trail records the version and never the secret. Tests:
`tests/test_derive.py` (no database) and `tests/test_derived_secrets.py` (11 end to end). **Not done:** the existing sites still hold stored
secrets until each is rotated, and the SQL rotation in `docs/KEYS.md` works only for those. `seed.py` and `seed_demo.py` still make stored-secret sites.

**Done when:** tests show the derivation is stable, differs per site and per version, that verification works for both
derived and legacy sites, and that rotating invalidates the old secret; `docs/KEYS.md` is updated; and a production hub
refuses to start without the master key.

### 5. A secret scanner before every commit (built)

**Problem.** Nothing stops a real secret being committed. `.env` and `.site-keys` are git-ignored, and the history was checked
by hand before the last push, but that depends on remembering. OWASP's secrets guidance asks for detection at the developer's
machine, with more than one tool where possible.

**Change.** A pre-commit hook that scans the staged changes, and the same scan in the Gitea pipeline so a commit made without the
hook is still caught. `detect-secrets` and `gitleaks` are the usual choices and OWASP names `detect-secrets` as mature; pick one
and check its current setup before relying on it.

**Care needed.** The repository has harmless look-alikes: the `hub:hub` password in `.env.example`, test keys such as `s3cret`,
and long example tokens in the docs. A baseline or allow list is needed or the hook gets switched off, which is worse than none.

**Built with gitleaks.** `.gitleaks.toml` extends the default rules and allowlists three things, each commented: `tests/`, `*.test.mjs`
and the `.env*.example` files. `.githooks/pre-commit` scans staged changes (local `gitleaks`, else its Docker image) and **fails if
neither exists** rather than skipping. CI runs the same scan over the full history. Enable the hook once per clone:
`git config core.hooksPath .githooks`. **Tried:** a staged file holding a fake AWS key and a GitHub-style token was blocked
(exit 1); the full history of 210 commits scans clean with the allowlist. Without it, the scan flagged five look-alikes.

**Care:** the `tests/` allowlist would also miss a real secret pasted into a test. Keep real values out of tests.

**Done when:** a deliberately fake secret in a throwaway commit is blocked, the existing history scans clean, and the allow list
is short and commented. (Met, except that the CI job has not yet run on GitHub.)

### 6. Production secrets as mounted files, not environment variables (built)

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

### 7. An audit trail for sites and secrets (built)

**Problem.** Issue actions are logged ("who muted that, and why is it quiet?"). Creating a site, changing its URL or browser origins,
and above all rotating a secret leave no record, so "who changed that and when?" has no answer.

**Change.** An append-only `audit_log` table (what, which site, when, and who, which is just "the console" while there is one
token) for site creation, URL and origin changes, secret rotation and revocation. **It never stores a secret value.**

**Care needed.** OWASP asks that audit logs resist tampering. The hub offers no way to edit or delete rows, and a copy should
leave the machine with the backups. Trustworthy timestamps come from the server clock, so the VPS must keep time in sync.

**Built.** `hub/audit.py` and an `audit_log` table (migration 0012). A row is added in the same transaction as the change it
describes, so the trail cannot miss a change or describe one that failed. Actions: `site_created`, `url_changed`,
`origins_changed`, `browser_key_issued`, `browser_key_revoked`, `secret_rotated`. A URL is stored without userinfo or query string,
and a key is never stored, only the fact it changed. The only route is `GET /api/sites/{id}/audit`; PUT, PATCH, DELETE and POST
return 405. The Sites tab has a History button per site. `tests/test_audit.py` covers each action, no-op patches writing nothing,
no secret in any row, and failed changes leaving no row. **Not done:** the trail is not yet copied off the machine with the
backups (it is in the database dump, which is already encrypted), and a database user that can run SQL can still edit it.

**Done when:** each of those actions writes a row, a test proves no row ever contains a secret, and the console can show the
trail for a site. (Met.)

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
(a timestamp in the signed body does that, with either scheme; **built** for the shared secret, see `docs/KEYS.md`).

**A middle path** if the hub ever holds many customers' secrets: keep HMAC, but store each secret encrypted with a key held
outside the database, so a database leak alone is not enough.

## Source

The secret-handling items follow the [OWASP Secrets Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html),
read in October 2026: central and least-privilege handling, regular and automated rotation, keys not stored beside the data they
protect, environment variables as a last resort, CI treated as production, secrets never logged, tamper-resistant audit logs,
leak detection early, and revoke then rotate then delete then record when one leaks.

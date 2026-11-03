# Continuous integration for Nightjar itself

> **Status: running on GitHub.** The workflow is `.github/workflows/ci.yml`. It first ran on pull request 11 and the console, hub and
> secret-scan jobs passed. The reporters job below was added after that run, so its first result is still to be seen.

This is the check on **Nightjar's own repository**, on GitHub. It is separate from the pipeline that deploys *client
apps*, which runs on Gitea and the staging host (planned, see `docs/PIPELINE.md` once written).

## What runs, on every push to `main` or a `feature/**` branch, and on every pull request

| Job | Steps | Same thing on a laptop |
|---|---|---|
| **Console** | `npm ci`, `npm test`, `npm run build`, then regenerate the API types and fail if they changed | `cd console && npm test && npm run build && npm run types && git diff --exit-code -- src/api` |
| **Hub** | the whole Python suite against a real Postgres, then the client reporter tests | `python -m unittest discover -s tests` and `python -m unittest discover -s client`, with the dev database up |
| **Reporters** | the Node, browser, PHP and Java reporters' own tests; a missing runtime fails the job instead of skipping | `node client/node/nightjar.test.mjs`, `node client/browser/nightjar.test.mjs`, `php client/php/test.php`, and `javac --release 11 -d out client/java/*.java && java -cp out nightjar.NightjarTest` |

- `npm test` is `tsc --noEmit` under `strict`, the console's rules, the snippet and alert wording tests, the grouping rules, and
  the CSS collision guard.
- The hub suite is about 340 tests: migrations (including that the models and migrations agree), auth (including that no route is
  open without the console token), fingerprinting, the alert policy, outbox, sender, receipts, cancel-on-acknowledge and uptime
  outages, and each server reporter against the real hub in Node, PHP and Java.
- **Two checks keep the generated API types honest.** `tests/test_openapi_snapshot.py` fails if the committed OpenAPI file differs
  from the hub, and the console job fails if `schema.d.ts` differs from that file. A hub change whose types were not regenerated
  therefore fails in one place or the other.

## Reading a failure

- **Console, "console/src/api is stale":** run `npm run types` in `console/` and commit `src/api`.
- **Hub, `test_the_committed_schema_matches_the_hub`:** run `python tools/export_openapi.py`, then `npm run types`, and commit both.
- **Hub, a reporter test reported as skipped:** the runtime was missing on the runner. The job prints Node, PHP and Java versions
  first so this is visible.
- **A test that passes locally and fails on the runner:** the tests were developed on Windows and the runner is Linux, so look
  first at paths, line endings, and anything that depends on the machine's time zone or installed tools.

## Settings worth turning on in GitHub

- **Branch protection on `main`:** require the `Console (TypeScript)` and `Hub (Python, Postgres)` checks to pass, and require a pull
  request. Without this the workflow reports problems but cannot stop a merge.
- **Actions permissions:** the workflow already asks for read-only access to the repository and needs nothing else. Leave the
  repository default at read-only.

## What is not verified

- **It has not run on GitHub.** The workflow passes `actionlint` (`pip install actionlint-py`, then `actionlint .github/workflows/ci.yml`)
  and each command passes locally, but the Linux runner, the Postgres service container and the preinstalled PHP have not been
  exercised. The first run is the real test.
- **The actions are pinned to major versions** (`actions/checkout@v4` and so on), not to commit hashes. Pinning to full hashes is
  stronger against a compromised action, and is a follow-up along with Dependabot to keep them current.
- **One Python version (3.13).** The project says 3.11 or newer, and 3.11 is not tested here.

## Planned: Dependabot

Not started. A `.github/dependabot.yml` so dependency and action updates arrive as pull requests that this workflow then checks:

- **`github-actions`**, weekly: keeps `actions/checkout`, `setup-node`, `setup-python`, `setup-java` current, and is the natural moment to pin them
  to full commit hashes (a tag can be moved; a hash cannot), which is stronger against a compromised action.
- **`pip`** for `requirements.txt`, **`npm`** for `console/` and `tools/typegen/` (two lock files), and **`docker`** for the `Dockerfile` base image.
- Open questions: group minor and patch updates into one weekly PR so a solo developer is not buried; whether security updates should open
  immediately while version updates wait for the week; and that `tools/typegen` must stay on TypeScript 5.9 while the console is on 7, so
  the TypeScript package needs an ignore rule there.
- Dependabot only proposes. Nothing merges by itself, and CI plus a read of the diff is still the gate. Alerts and security updates are also a switch in the
  repository settings (Security, then Code security), separate from the file.

## Deliberately not here

- **No deploy.** CI only checks. Deploying the hub is a manual step in `DEPLOY.md` for now.
- **No secrets.** Nothing in the workflow needs one, and none should be added: the tests use fakes for Pushover and make
  throwaway databases.
- **No secret scanner yet.** That is one of the data-protection changes in `docs/DATA-PROTECTION.md`.

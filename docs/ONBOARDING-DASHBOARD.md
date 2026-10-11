# Onboarding dashboard (planned)

> **Status: built (2026-10-11), as the console's Sites tab.** A new client app can be added, wired up and checked without a
> terminal. Written as `console/src/views/Sites.tsx` with the snippet generator in `console/src/snippets.ts`. The open questions at the
> end are answered below the table.

## The problem

Adding a client app today means a `curl` with the console token, copying the key and secret out of the reply, and
working out which reporter and which environment variables that app's stack needs. Nothing then confirms the app is
actually reporting. A site that is wired wrongly looks exactly like a site that has had no errors, which is the
silence this project exists to distrust.

## What exists

| Piece | State |
|---|---|
| `POST /api/sites` creates a site and returns the key and secret once | Built |
| `PATCH /api/sites/{id}` sets the `url` and the browser origins | Built |
| The Overview names production sites with no `url` | Built |
| `GET /api/sites/health` has `last_event`, `last_check`, `monitored` per site | Built |
| A reporter for each stack, with one set of environment variables | Built (`docs/REPORTERS.md`) |
| **A screen for any of it** | **Built** (Sites tab) |

## The screen

A new **Sites** tab, or an **Add a site** button on the Overview, with three steps.

1. **Details.** Client (an existing one, or a new name), site name, production or staging, the live URL (optional,
   with a note that staging cannot be probed from the VPS), and the pages allowed to report errors from a browser
   (optional).
2. **Keys, shown once.** The ingest key and secret with copy buttons, and a warning that the secret is not shown
   again. Beside them, a snippet for the site's stack with those values already filled in: Python, Node, PHP, Java or
   browser, and the Gitea secret names (`NIGHTJAR_KEY`, `NIGHTJAR_SECRET`) to store them under.
3. **Waiting for the first report.** The screen polls until something arrives and then says so: "last error 4
   seconds ago", "last check 12 seconds ago". If nothing arrives after a few minutes it says what to check, instead
   of staying green. A button sends a test event from the console so a new site can be proven end to end.

A list of every site below it, with its URL, whether it is being checked, when it last reported, and edit controls
for the URL and the browser origins.

## What the hub needs

- `GET /api/sites` to list sites with their client, URL, browser origins and ingest key. The key is an address, not a
  secret (`docs/KEYS.md`), so listing it is fine. **The secret is never returned by a list.**
- `POST /api/sites/{id}/rotate-secret`, which replaces the SQL statement `KEYS.md` currently describes. It returns
  the new secret once.
- `POST /api/sites/{id}/test-event`, which sends one `warning` through the real ingest path and returns what the
  hub did with it, so "wired correctly" is a fact and not a hope.

## How to build it

- **Written in TypeScript** (`.tsx`), as the migration plan says new console files should be, which means the
  tooling step from [`TYPESCRIPT-MIGRATION.md`](TYPESCRIPT-MIGRATION.md) comes first. It is small and is the first
  phase of that plan anyway.
- Snippets are generated in the console from the site's values, so there is one place to update when a reporter
  changes.
- Tests: the hub endpoints get database tests like `tests/test_sites.py`; the snippet generator is a pure function
  with a Node test like the other console modules.

## Rules

1. The secret is shown once, in the create or rotate response, and kept out of anything the console stores or logs.
2. A site that has never reported says so. It never shows a healthy state it has not earned.
3. Nothing here weakens an alert or changes severity. It adds sites; it does not touch routing.

## Out of scope

Deleting a site or a client from the screen (data loss deserves its own care), renaming a site (tickets and staging
pairing look sites up by name), and per-client logins (decision #7).

## Open questions

1. **Create the client separately, or inline?** Inline is quicker; a separate step avoids a typo creating a second
   "Kubasu Motors".
2. **Should the test event be allowed on production sites?** It is a `warning` (P3, digest only), so it should be
   safe, but it does put a row in a real client's issue list. It could be marked as a test and resolved at once.
3. **Where does the Gitea secret get stored?** The screen can only show the names. Writing into Gitea itself would
   need a Gitea token, which is a separate decision.

## Answers, as built

1. **Client: inline.** The client field is a free text box with the existing clients offered as suggestions, so a typo is the only
   way to make a second "Kubasu Motors". Quicker than a separate step; revisit if it happens.
2. **Test event: allowed on production, and resolved at once.** It is a `warning` (P3, digest only), marked by its type
   `NightjarTestEvent`, and **left out of a site's "last reported" time**. Otherwise pressing the button would make a site whose app
   was never wired up look as if it had reported. The panel says a test proves the hub side only. A second test re-opens the
   resolved issue as a `regression`, which for a P3 is only another digest line.
3. **Gitea secret: names only.** The screen shows `NIGHTJAR_URL`, `NIGHTJAR_KEY` and `NIGHTJAR_SECRET` to store; writing them
   needs a Gitea token, still a separate decision.

## Things found by running it

- **The idle screen would have destroyed the form and the secret.** It starts after 5 minutes with no input and unmounts the tab.
  The tab now asks the shell to hold it while a form is open or a secret is showing. Any future screen that holds unrecoverable
  state should do the same through `setIdleHold`.
- **A secret left on screen clears itself after 10 minutes**, so it is not readable on an unattended display.
- The `.chip` class has no styles anywhere, so the Incidents filter buttons are unstyled. The Sites tab uses its own `sx-chip`;
  Incidents is left as it was.

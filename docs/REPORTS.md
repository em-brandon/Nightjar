# Reports: history, trends and client summaries (planned)

> **Status: planned, not started.** A spec, written so the build is a straight line. Nothing here exists yet.

## The gap

The console answers "what needs me now". It cannot answer "how did March go for this client", "did the deploy on the 12th
cause the error spike", or "how long do I take to acknowledge a P1". The data for most of this is already stored. What is
missing is a place to read it, and a record that outlives pruning.

## Decision: build it in the console, not Grafana

`docs/DECISIONS.md` rejects Grafana-inside-Nightjar. This is not that. It is a fixed set of views over data Nightjar
already owns, with no query builder and no dashboard editor, so there is no second service to run, secure or back up. If a
question these views cannot answer comes up often, point Grafana at a read-only Postgres role then.

## What exists today

| Need | Source | Note |
|---|---|---|
| Uptime % per site | `checks.ok`, `checked_at` | pruned after 100 days (`probe.py` `RETENTION`) |
| Response time | `checks.latency_ms` | `NULL` on a failed check |
| Errors per day | `events.occurred_at` | **never pruned today**: `_prune()` does not touch `events`, so the table grows without bound |
| Alerts per week | `notifications` (`created_at`, `change`, `severity`, `status`) | failed sends included |
| Time to acknowledge | `notifications.sent_at` to `acked_at` (P1 with a receipt), and `issue_actions` (`ack`, `resolve`) from the console | use both; `acked_at` only exists for receipted pushes |
| Unanswered P1s | `notifications.expired_at` | |
| Deploy markers | `deploys` (`ref`, `status`, `created_at`) | no finish time; markers only |
| Grouping by client | `sites.client_id` to `clients` | |

## The one new table: a daily rollup

`daily_stats`, one row per site per UTC day:

| Column | Meaning |
|---|---|
| `site_id`, `day` | primary key together |
| `checks_total`, `checks_ok` | for uptime % |
| `latency_avg_ms`, `latency_p95_ms` | over successful checks only |
| `events` | error events that day |
| `issues_opened` | issues whose `first_seen` fell on the day |
| `alerts_sent`, `alerts_failed`, `p1_expired` | from `notifications` |

Why it exists: `checks` is cut at 100 days, and `events` will need pruning too (it currently grows unbounded). A rollup is
a few hundred bytes per site per day, so history for a report can be kept indefinitely.

Rules:
1. **Written before it is pruned.** The probe's prune step first rolls up every complete day older than yesterday, then deletes.
   Rolling up is idempotent (an upsert), so a crash between the two steps repeats safely.
2. **Today is computed live** from raw rows, never read from the rollup, so a report is never a day behind.
3. **Backfill once** from existing `checks` and `events` in the migration or a one-off command.
4. Migration `0011`, with the generated OpenAPI snapshot test updated alongside.

## API (read-only, behind the console token)

- `GET /api/reports/summary?client=<id>&from=<date>&to=<date>`: per site, uptime %, latency avg and p95, errors, issues
  opened, alerts sent/failed, P1s expired, median time to acknowledge.
- `GET /api/reports/series?site=<id>&metric=<name>&from=&to=`: daily points for a chart, plus the `deploys` in range as markers.
- Both clamp the range (for example 400 days) and validate dates, so a bad request cannot scan the table.

## Console: a Reports tab

- Client dropdown and a date range (this month, last month, last 90 days).
- Per site: uptime %, a latency line, an errors-per-day bar chart with deploy markers, alerts per week.
- Chart library: uPlot or Chart.js, whichever is smaller once built. No query language.
- **Print view** for the client: a single page with the client's name, the period, one row per site, and no console chrome,
  so the browser's "Save as PDF" is the export. No server-side PDF engine.

## Client-facing privacy

A report shows counts and percentages, never issue titles, stack frames, URLs with tokens, or `last_error` text. Those can carry
sensitive detail (see the comment on `Check.error`). The print view reads from the same summary endpoint, which has no such fields.

## Out of scope

Ad hoc queries, custom panels, alerting from a chart, per-route latency, live dashboards for clients, scheduled email delivery
(until the digest in chunk 4 exists), and Prometheus. This is history for one operator and a monthly page for each client.

## Build order

1. Migration `0011` and the rollup writer, wired into `_prune()`, with tests (idempotent, crash-safe, today stays live).
2. Backfill command.
3. `/api/reports/*` with tests, including the range clamp and the no-sensitive-fields rule.
4. Reports tab and print view, with console tests like the existing `*.test.mjs`.
5. Prune `events` (separate decision: keep raw events for N days now that history lives in the rollup).

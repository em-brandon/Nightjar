"""Daily rollup: one row per site per complete UTC day, written before the raw rows are pruned.

`checks` is cut after RETENTION days and `events` will need pruning too, so a report that reaches back
further than that can only be built from here. A rollup is a few hundred bytes per site per day.

Rules (docs/REPORTS.md):
  * only complete days: today is never rolled up, so a row here is final
  * idempotent: every run is an upsert, so a crash between this and the prune repeats safely
  * the first run is also the backfill: with nothing rolled up yet it starts at the oldest raw row
  * a day with no records has no row. A missing row means "no data", not zero.
"""

import logging
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("nightjar.rollup")

_CHECKS = text(
    """
    SELECT site_id, (checked_at AT TIME ZONE 'UTC')::date AS day,
           count(*) AS checks_total,
           count(*) FILTER (WHERE ok) AS checks_ok,
           round(avg(latency_ms) FILTER (WHERE ok AND latency_ms IS NOT NULL))::int AS latency_avg_ms,
           round((percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
                  FILTER (WHERE ok AND latency_ms IS NOT NULL))::numeric)::int AS latency_p95_ms
    FROM checks
    WHERE checked_at >= :start AND checked_at < :end
    GROUP BY 1, 2
    """
)
_EVENTS = text(
    """
    SELECT i.site_id, (e.occurred_at AT TIME ZONE 'UTC')::date AS day, count(*) AS events
    FROM events e JOIN issues i ON i.id = e.issue_id
    WHERE e.occurred_at >= :start AND e.occurred_at < :end
    GROUP BY 1, 2
    """
)
_ISSUES = text(
    """
    SELECT site_id, (first_seen AT TIME ZONE 'UTC')::date AS day, count(*) AS issues_opened
    FROM issues
    WHERE first_seen >= :start AND first_seen < :end
    GROUP BY 1, 2
    """
)
_ALERTS = text(
    """
    SELECT i.site_id, (n.created_at AT TIME ZONE 'UTC')::date AS day,
           count(*) FILTER (WHERE n.status = 'sent') AS alerts_sent,
           count(*) FILTER (WHERE n.status = 'failed') AS alerts_failed,
           count(*) FILTER (WHERE n.expired_at IS NOT NULL) AS p1_expired
    FROM notifications n JOIN issues i ON i.id = n.issue_id
    WHERE n.created_at >= :start AND n.created_at < :end
    GROUP BY 1, 2
    """
)
_EARLIEST = text(
    """
    SELECT least(
        (SELECT min(checked_at) FROM checks),
        (SELECT min(occurred_at) FROM events),
        (SELECT min(first_seen) FROM issues),
        (SELECT min(created_at) FROM notifications)
    )
    """
)
_LATEST = text("SELECT max(day) FROM daily_stats")

_FIELDS = (
    "checks_total",
    "checks_ok",
    "latency_avg_ms",
    "latency_p95_ms",
    "events",
    "issues_opened",
    "alerts_sent",
    "alerts_failed",
    "p1_expired",
)
_UPSERT = text(
    f"""
    INSERT INTO daily_stats (site_id, day, {", ".join(_FIELDS)})
    VALUES (:site_id, :day, {", ".join(":" + f for f in _FIELDS)})
    ON CONFLICT (site_id, day) DO UPDATE SET {", ".join(f"{f} = EXCLUDED.{f}" for f in _FIELDS)}
    """
)


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


async def rollup(session: AsyncSession, now: datetime | None = None) -> int:
    """Roll every complete day not yet rolled up. Commits, so the result is durable before the caller
    deletes anything. Returns the number of site-days written."""
    today = (now or datetime.now(UTC)).astimezone(UTC).date()

    latest = (await session.execute(_LATEST)).scalar()
    if latest is not None:
        # The last rolled day is rolled again: harmless, and it repairs a day that was cut short.
        start_day = latest
    else:
        earliest = (await session.execute(_EARLIEST)).scalar()
        if earliest is None:
            return 0
        start_day = earliest.astimezone(UTC).date()
    if start_day >= today:
        return 0

    window = {"start": _midnight(start_day), "end": _midnight(today)}
    rows: dict[tuple[int, date], dict] = {}
    for query in (_CHECKS, _EVENTS, _ISSUES, _ALERTS):
        for record in (await session.execute(query, window)).mappings():
            row = rows.setdefault(
                (record["site_id"], record["day"]),
                {"site_id": record["site_id"], "day": record["day"], **dict.fromkeys(_FIELDS, 0)},
            )
            row.update({k: v for k, v in record.items() if k in _FIELDS})
    # The latency columns are nullable (no successful check means no latency), not zero.
    for row in rows.values():
        if not row["checks_ok"]:
            row["latency_avg_ms"] = row["latency_p95_ms"] = None

    if rows:
        await session.execute(_UPSERT, list(rows.values()))
    await session.commit()
    log.info("rolled up %d site-days from %s to %s", len(rows), start_day, today - timedelta(days=1))
    return len(rows)

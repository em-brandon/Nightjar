"""Demo data, so the console can be looked at with something in it.

    python seed_demo.py            add the demo clients, sites, issues, alerts, tickets and deploys
    python seed_demo.py --remove   take every bit of it out again
    python seed_demo.py --reset    remove, then add

Everything belongs to a client whose name ends in " (demo)", which is how --remove finds it, and nothing else is
touched. It writes straight to the database in DATABASE_URL, so it is for a development database.

Two things are deliberate, because the hub is running while you look at this:

  * Every monitored demo site points at the hub's own /healthz. The probe really does request each site's URL
    every minute, and a URL that failed would open a real P1 and queue a real alert. This one always passes. A
    site shown as down is down because of a seeded open P1, not because a probe failed.
  * No alert is left waiting to be sent. Seeded alerts are in a final state (sent, failed, expired), and the
    one shown as "retrying" is scheduled for the year 2100, so a hub with Pushover configured never pushes a
    demo alert to your phone. None of them carries a receipt either, so nothing asks Pushover about them.
"""

import asyncio
import random
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, insert, select

from hub.db import SessionLocal
from hub.migrate import upgrade
from hub.models import Check, Client, Deploy, Event, Issue, IssueAction, Notification, Site, StackReport, Ticket

SUFFIX = " (demo)"
# The probe requests this: the hub itself, so it always answers and never raises a real alert.
HEALTH = "http://localhost:8000/healthz?demo={name}"
FAR_FUTURE = datetime(2100, 1, 1, tzinfo=UTC)
NOW = datetime.now(UTC)
rng = random.Random(7)


def ago(**kw) -> datetime:
    return NOW - timedelta(**kw)


# (client, [(site name, env, monitored)]) in the order they should read
CLIENTS = [
    ("Acacia Hardware", [("acacia-hardware", "production", True), ("acacia-hardware-staging", "staging", True)]),
    ("Baobab Academy", [("baobab-academy", "production", True), ("baobab-academy-staging", "staging", False)]),
    (
        "Savanna Safaris",
        [
            ("savanna-safaris", "production", True),
            ("savanna-booking", "production", True),
            ("savanna-booking-staging", "staging", True),
        ],
    ),
    ("Pwani Agrovet", [("pwani-agrovet", "production", False)]),
]

# Days ago -> hours of failed checks, to give the 90-day ribbon some history: an outage and a few rough patches
BAD_DAYS = {
    "acacia-hardware": {12: range(2, 9), 40: range(14, 16), 71: range(3, 5)},
    "baobab-academy": {5: range(10, 12), 33: range(1, 3)},
    "savanna-safaris": {20: range(6, 8)},
    "savanna-booking": {},
}


async def remove(session) -> int:
    ids = list((await session.scalars(select(Client.id).where(Client.name.like(f"%{SUFFIX}")))).all())
    if not ids:
        return 0
    site_ids = list((await session.scalars(select(Site.id).where(Site.client_id.in_(ids)))).all())
    # tickets only point at a site loosely (SET NULL), so they would be left behind by the cascade
    await session.execute(delete(Ticket).where(Ticket.site_id.in_(site_ids)))
    await session.execute(delete(Client).where(Client.id.in_(ids)))  # cascades to sites, issues, events, checks, deploys, stacks, alerts
    await session.commit()
    return len(ids)


async def clash(session) -> list[str]:
    """Site names are not unique in the schema, and promotion pairs staging with production by NAME, so a demo site
    sharing a real site's name would be mistaken for it. Refuse rather than pollute."""
    wanted = [name for _, entries in CLIENTS for name, _, _ in entries]
    return list((await session.scalars(select(Site.name).where(Site.name.in_(wanted)))).all())


def fingerprint(site: str, text: str) -> str:
    import hashlib

    return hashlib.sha256(f"{site}|{text}".encode()).hexdigest()[:32]


async def add(session) -> dict[str, Site]:
    sites: dict[str, Site] = {}
    for client_name, entries in CLIENTS:
        client = Client(name=client_name + SUFFIX, contact_email=f"hello@{client_name.lower().replace(' ', '')}.example")
        session.add(client)
        await session.flush()
        for name, env, monitored in entries:
            site = Site(client_id=client.id, name=name, env=env, url=HEALTH.format(name=name) if monitored else None)
            session.add(site)
            sites[name] = site
    await session.flush()

    # ---------------------------------------------------------------- uptime history
    rows = []
    for name, site in sites.items():
        if site.url is None:
            continue
        bad = BAD_DAYS.get(name, {})
        for day in range(90, 0, -1):
            for hour in range(24):
                failing = hour in bad.get(day, ())
                at = (NOW - timedelta(days=day)).replace(hour=hour, minute=7, second=0, microsecond=0)
                rows.append(dict(site_id=site.id, checked_at=at, ok=not failing, status_code=503 if failing else 200,
                                 latency_ms=None if failing else rng.randint(140, 420), error="HTTP 503" if failing else None))
        # the last 24 hours at 5-minute spacing, so the sparkline has something to draw
        for i in range(288, 0, -1):
            rows.append(dict(site_id=site.id, checked_at=ago(minutes=i * 5), ok=True, status_code=200,
                             latency_ms=rng.randint(140, 380), error=None))
    for i in range(0, len(rows), 2000):
        await session.execute(insert(Check), rows[i : i + 2000])

    # ---------------------------------------------------------------- issues, events, actions
    def issue(site: str, title: str, level: str, severity: str, status: str, count: int, first: datetime, last: datetime, culprit: str = "") -> Issue:
        row = Issue(site_id=sites[site].id, fingerprint=fingerprint(site, title), title=title, culprit=culprit, level=level,
                    severity=severity, status=status, event_count=count, first_seen=first, last_seen=last)
        session.add(row)
        return row

    def events(row: Issue, n: int, within: timedelta, etype: str, message: str):
        for _ in range(n):
            at = NOW - timedelta(seconds=rng.randint(0, int(within.total_seconds())))
            session.add(Event(issue_id=row.id, occurred_at=at, payload={"exception_type": etype, "message": message, "level": row.level}))

    p1_exit = issue("acacia-hardware", "ProcessExit: the checkout service exited", "fatal", "P1", "open", 3, ago(minutes=24), ago(minutes=9), "checkout.worker:run")
    p2_cart = issue("acacia-hardware", "KeyError: 'cart_id'", "error", "P2", "open", 214, ago(days=3), ago(minutes=4), "cart.views:add_item")
    p2_pay = issue("acacia-hardware", "TimeoutError: payment gateway did not answer", "error", "P2", "muted", 1843, ago(days=40), ago(hours=6), "payments.client:charge")
    p3_dep = issue("acacia-hardware", "DeprecationWarning: datetime.utcnow() is deprecated", "warning", "P3", "resolved", 58, ago(days=9), ago(days=2))
    p1_old = issue("acacia-hardware", "UptimeCheckFailed: the site is not responding to uptime checks", "fatal", "P1", "resolved", 1, ago(days=12, hours=-2), ago(days=12, hours=-2))
    p2_db = issue("baobab-academy", "OperationalError: too many connections", "error", "P2", "open", 37, ago(hours=5), ago(minutes=2), "db.pool:acquire")
    p2_mail = issue("baobab-academy", "SMTPException: mailbox full", "error", "P2", "snoozed", 12, ago(days=2), ago(hours=20), "mail.send:deliver")
    p3_slow = issue("baobab-academy", "SlowQueryWarning: report took 11s", "warning", "P3", "open", 9, ago(days=1), ago(hours=3))
    p1_sav = issue("savanna-safaris", "UptimeCheckFailed: the site is not responding to uptime checks", "fatal", "P1", "resolved", 1, ago(days=20, hours=-1), ago(days=20, hours=-1))
    p3_img = issue("savanna-safaris", "ImageError: thumbnail could not be generated", "warning", "P3", "resolved", 4, ago(days=6), ago(days=5))
    p2_book = issue("savanna-booking-staging", "ValueError: unknown park code 'MSAI'", "error", "P2", "open", 6, ago(hours=2), ago(minutes=40), "booking.parks:lookup")
    p3_agro = issue("pwani-agrovet", "JSONDecodeError: bad price feed", "warning", "P3", "open", 3, ago(days=1), ago(hours=7))
    await session.flush()

    events(p1_exit, 3, timedelta(minutes=24), "ProcessExit", "the checkout service exited")
    events(p2_cart, 6, timedelta(hours=2), "KeyError", "'cart_id'")
    # rising: many in the last 30 minutes, few in the 30 before, so the "Errors rising" panel has something to show
    for minutes_ago, n in ((range(0, 30), 12), (range(30, 60), 3)):
        for _ in range(n):
            session.add(Event(issue_id=p2_db.id, occurred_at=ago(minutes=rng.choice(list(minutes_ago)), seconds=rng.randint(0, 59)),
                              payload={"exception_type": "OperationalError", "message": "too many connections", "level": "error"}))
    for row in (p2_pay, p3_dep, p1_old, p2_mail, p3_slow, p1_sav, p3_img, p2_book, p3_agro):
        events(row, 1, timedelta(hours=1), "Demo", row.title)

    def action(row: Issue, what: str, detail: str = "", at: datetime | None = None):
        session.add(IssueAction(issue_id=row.id, action=what, detail=detail, created_at=at or NOW))

    action(p2_cart, "ack", "", ago(hours=20))
    action(p2_pay, "mute", "", ago(days=30))
    action(p3_dep, "resolve", "", ago(days=2))
    action(p1_old, "ack", "phone", ago(days=12, hours=-3))
    action(p1_old, "resolve", "auto: 5 checks in a row passed", ago(days=12, hours=-9))
    action(p2_mail, "snooze", "24h", ago(hours=20))
    p2_mail.snoozed_until = NOW + timedelta(hours=4)
    action(p1_sav, "resolve", "auto: 5 checks in a row passed", ago(days=20))
    action(p3_img, "resolve", "", ago(days=5))

    # ---------------------------------------------------------------- alerts, every one in a final state
    def alert(row: Issue, site: str, severity: str, change: str, priority: int, status: str, created: datetime, **kw):
        values = dict(issue_id=row.id, change=change, changed_at=created, severity=severity, priority=priority, site=site,
                      title=row.title, status=status, attempts=1, next_attempt_at=created, created_at=created)
        values.update(kw)
        session.add(Notification(**values))

    alert(p1_exit, "acacia-hardware", "P1", "new", 2, "sent", ago(minutes=24), sent_at=ago(minutes=24))  # still awaiting acknowledgement
    alert(p1_old, "acacia-hardware", "P1", "new", 2, "sent", ago(days=12, hours=-2), sent_at=ago(days=12, hours=-2), acked_at=ago(days=12, hours=-3))
    alert(p1_sav, "savanna-safaris", "P1", "new", 2, "sent", ago(days=20, hours=-1), sent_at=ago(days=20, hours=-1), expired_at=ago(days=20, hours=-1.5))  # nobody answered
    alert(p2_cart, "acacia-hardware", "P2", "new", 0, "sent", ago(days=3), sent_at=ago(days=3))
    alert(p2_cart, "acacia-hardware", "P2", "spike", -1, "sent", ago(hours=21), sent_at=ago(hours=21))  # quiet hours: delivered without sound
    alert(p2_db, "baobab-academy", "P2", "new", 0, "failed", ago(hours=5), attempts=6, last_error="Pushover is not configured (set PUSHOVER_TOKEN and PUSHOVER_USER)")
    alert(p2_book, "savanna-booking-staging", "P2", "new", 0, "pending", ago(hours=2), attempts=2, next_attempt_at=FAR_FUTURE, last_error="Pushover timed out")
    await session.flush()

    # ---------------------------------------------------------------- client requests
    session.add_all([
        Ticket(site_id=sites["acacia-hardware"].id, kind="change", subject="Swap the homepage banner for the October offer", sender="amina@acacia.example", created_at=ago(days=4), due_at=ago(days=1)),
        Ticket(site_id=sites["baobab-academy"].id, kind="report", subject="Parent portal shows last term's fees", sender="office@baobab.example", created_at=ago(hours=6), due_at=NOW + timedelta(days=2)),
        Ticket(site_id=sites["savanna-safaris"].id, kind="access", subject="New marketing hire needs editor access", sender="joy@savanna.example", created_at=ago(days=6), status="closed", closed_at=ago(days=5)),
    ])

    # ---------------------------------------------------------------- deploys
    def deploy(site: str, ref: str, status: str, created: datetime, progress: int | None = None):
        session.add(Deploy(site_id=sites[site].id, ref=ref, status=status, progress=progress, created_at=created))

    deploy("acacia-hardware", "main@a41c9e2", "success", ago(days=1))
    deploy("acacia-hardware", "main@77b02de", "failed", ago(days=3))
    deploy("acacia-hardware", "main@2f9a118", "success", ago(days=8))
    deploy("baobab-academy", "main@c05de71", "success", ago(days=2))
    deploy("savanna-safaris", "main@91ab3c0", "success", ago(days=5))
    deploy("savanna-booking", "main@5e7d210", "success", ago(days=4))
    deploy("acacia-hardware-staging", "develop@e19f6a3", "success", ago(hours=3))
    deploy("savanna-booking-staging", "develop@3c82b9d", "running", ago(minutes=2), progress=60)
    deploy("savanna-booking-staging", "develop@1d44e5f", "success", ago(days=1))

    # ---------------------------------------------------------------- staging stacks
    def stack(site: str, name: str, state: str, mem: int, limit: int | None, services: list[dict]):
        for i in range(12, -1, -1):
            session.add(StackReport(site_id=sites[site].id, reported_at=ago(minutes=i), stack=name, state=state,
                                    mem_mb=mem + rng.randint(-30, 30), mem_limit_mb=limit, cpu_pct=round(rng.uniform(2, 14), 1),
                                    host_mem_mb=15_800, disk_mb=2_300, restarts=0, host_disk_total_mb=500_000, host_disk_free_mb=312_000,
                                    services=services))

    stack("acacia-hardware-staging", "acacia", "running", 640, 1024, [
        {"name": "web", "state": "running", "mem_mb": 310, "restarts": 0}, {"name": "db", "state": "running", "mem_mb": 330, "restarts": 0}])
    stack("savanna-booking-staging", "savanna-booking", "degraded", 980, None, [
        {"name": "web", "state": "running", "mem_mb": 410, "restarts": 0},
        {"name": "worker", "state": "unhealthy", "mem_mb": 570, "restarts": 3, "note": "queue backlog growing"}])

    await session.commit()
    return sites


async def main(argv: list[str]) -> None:
    async with SessionLocal() as session:
        if "--remove" in argv or "--reset" in argv:
            print(f"removed {await remove(session)} demo clients")
            if "--remove" in argv:
                return
        elif await session.scalar(select(Client.id).where(Client.name.like(f"%{SUFFIX}")).limit(1)):
            print("demo data is already there. Use --reset to rebuild it, or --remove to take it out.")
            return
        if used := await clash(session):
            print(f"not seeding: these site names already exist and would be confused with the demo ones: {', '.join(used)}")
            return
        sites = await add(session)
    print(f"added {len(CLIENTS)} demo clients and {len(sites)} sites, with issues, alerts, requests, deploys and staging stacks.")
    print("Remove it with: python seed_demo.py --remove")


# Blocking, and Alembic runs its own event loop, so it goes before ours.
upgrade()
asyncio.run(main(sys.argv[1:]))

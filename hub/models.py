import secrets
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _key() -> str:
    return secrets.token_urlsafe(24)


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    sites: Mapped[list["Site"]] = relationship(back_populates="client")


class Site(Base):
    """One deployed app or website you look after."""

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    env: Mapped[str] = mapped_column(String(20), default="production")

    # What the uptime probe requests. None means "not monitored": the site still
    # reports crashes, it just has no health checks or uptime ribbon.
    url: Mapped[str | None] = mapped_column(String(500))

    # goes in the ingest URL — identifies the sender
    ingest_key: Mapped[str] = mapped_column(String(64), unique=True, default=_key)
    # signs the request body — proves the sender is really them
    ingest_secret: Mapped[str] = mapped_column(String(64), default=_key)

    # Browser reporting. A page cannot keep a secret, so a browser gets this separate PUBLIC key
    # instead, which can only report errors (never deploys, checks or anything else) and only
    # from the origins listed. None means browser reporting is off for the site. It is not the
    # ingest key: leaking it must not open the signed routes.
    public_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    allowed_origins: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    client: Mapped["Client"] = relationship(back_populates="sites")


class Issue(Base):
    """A group of events that share a fingerprint. This is what you see and act on."""

    __tablename__ = "issues"
    __table_args__ = (
        UniqueConstraint("site_id", "fingerprint", name="uq_issue_site_fingerprint"),
        Index("ix_issues_open", "status", "last_seen"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    fingerprint: Mapped[str] = mapped_column(String(64))

    title: Mapped[str] = mapped_column(String(300))
    culprit: Mapped[str] = mapped_column(String(300), default="")
    level: Mapped[str] = mapped_column(String(20), default="error")
    severity: Mapped[str] = mapped_column(String(4), default="P2")

    # open | resolved | muted | snoozed
    status: Mapped[str] = mapped_column(String(20), default="open")
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    event_count: Mapped[int] = mapped_column(BigInteger, default=0)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    site: Mapped["Site"] = relationship()


class Event(Base):
    """One raw occurrence. Prune these on a schedule; issues are the durable record."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_issue_time", "issue_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"))
    payload: Mapped[dict] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Check(Base):
    """One uptime probe result. Prune on a schedule; the ribbon needs ~90 days."""

    __tablename__ = "checks"
    __table_args__ = (Index("ix_checks_site_time", "site_id", "checked_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ok: Mapped[bool] = mapped_column(Boolean)
    status_code: Mapped[int | None] = mapped_column(Integer)
    # None on failure: a timeout has no meaningful latency
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # exception class name only, never the message: it can carry the URL
    error: Mapped[str | None] = mapped_column(String(100))


class Ticket(Base):
    """A client request: a change-over, an incident report, or an access ask.

    Separate from crashes because the clock is a promise to a person, not an
    error rate. Email intake (chunk 4) will create these; until then they come
    in through POST /api/tickets."""

    __tablename__ = "tickets"
    __table_args__ = (Index("ix_tickets_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # SET NULL, not CASCADE: deleting a site must not erase a client's request
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"))
    # change | report | access
    kind: Mapped[str] = mapped_column(String(20))
    subject: Mapped[str] = mapped_column(String(300))
    sender: Mapped[str] = mapped_column(String(255), default="")
    # open | closed
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    site: Mapped["Site | None"] = relationship()


class Deploy(Base):
    """One deploy status report from CI. The env comes from the site, so a
    staging deploy uses the staging site's key."""

    __tablename__ = "deploys"
    __table_args__ = (Index("ix_deploys_site_time", "site_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    ref: Mapped[str] = mapped_column(String(120), default="")  # e.g. main@1f9c4a2
    # running | success | failed
    status: Mapped[str] = mapped_column(String(20))
    # 0 to 100 while running, when the pipeline reports it
    progress: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    site: Mapped["Site"] = relationship()


class IssueAction(Base):
    """What a human did to an issue, and when. The audit trail: this is how you
    answer "who muted that, and why is it quiet?" weeks later."""

    __tablename__ = "issue_actions"
    __table_args__ = (Index("ix_actions_issue_time", "issue_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"))
    # ack | resolve | mute | snooze | reopen
    action: Mapped[str] = mapped_column(String(20))
    detail: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StackReport(Base):
    """One snapshot of a staging stack (a Docker Compose project) from the host
    it runs on. The hub cannot see that machine's Docker, and must not be given
    access to it, so a small reporter on the host pushes these in. Pruned on a
    short schedule: this is a live view, not a metrics store."""

    __tablename__ = "stack_reports"
    __table_args__ = (Index("ix_stack_site_time", "site_id", "reported_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    stack: Mapped[str] = mapped_column(String(120))
    # running | stopped | degraded
    state: Mapped[str] = mapped_column(String(20))
    mem_mb: Mapped[int | None] = mapped_column(Integer)
    # None means the stack has no memory limit, which is worth flagging
    mem_limit_mb: Mapped[int | None] = mapped_column(Integer)
    cpu_pct: Mapped[float | None] = mapped_column(Float)
    host_mem_mb: Mapped[int | None] = mapped_column(Integer)
    # Volumes plus each container's writable layer. Images are shared between
    # stacks, so they are deliberately not attributed to any one of them.
    disk_mb: Mapped[int | None] = mapped_column(Integer)
    # Cumulative restart count across the stack's containers. The feed works
    # out the recent delta; one number on its own says nothing about a loop.
    restarts: Mapped[int | None] = mapped_column(Integer)
    host_disk_total_mb: Mapped[int | None] = mapped_column(Integer)
    host_disk_free_mb: Mapped[int | None] = mapped_column(Integer)
    services: Mapped[list] = mapped_column(JSONB, default=list)


class MediaReport(Base):
    """One snapshot of the home media server (Jellyfin), pushed in by a reporter on
    the host it runs on. The hub cannot reach the home network, so it never asks.
    A live view, pruned on a short schedule; the durable record is MediaPlay."""

    __tablename__ = "media_reports"
    __table_args__ = (Index("ix_media_site_time", "site_id", "reported_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # False means the reporter is alive but Jellyfin is not answering. A reporter
    # that is not alive sends nothing, and shows as stale instead.
    up: Mapped[bool] = mapped_column(Boolean)
    version: Mapped[str | None] = mapped_column(String(40))
    host_disk_total_mb: Mapped[int | None] = mapped_column(Integer)
    host_disk_free_mb: Mapped[int | None] = mapped_column(Integer)
    sessions: Mapped[list] = mapped_column(JSONB, default=list)


class MediaPlay(Base):
    """One viewing: who watched what, on which device, and for how long. This is the
    history, and it is personal data, so it is pruned after `media_history_days`.

    A play continues while the same session keeps reporting the same item; a gap
    longer than PLAY_GAP (see hub/media.py) starts a new one, so pausing for lunch
    and coming back is one viewing and watching it again tomorrow is another."""

    __tablename__ = "media_plays"
    __table_args__ = (
        Index("ix_play_site_started", "site_id", "started_at"),
        Index("ix_play_key", "site_id", "session_id", "item_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    session_id: Mapped[str] = mapped_column(String(80))
    item_id: Mapped[str] = mapped_column(String(64))
    user_name: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(200))
    series: Mapped[str | None] = mapped_column(String(200))
    episode: Mapped[str | None] = mapped_column(String(20))
    kind: Mapped[str | None] = mapped_column(String(20))
    client: Mapped[str | None] = mapped_column(String(120))
    device: Mapped[str | None] = mapped_column(String(120))
    play_method: Mapped[str | None] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Time actually spent watching: paused time and reporter gaps are not counted.
    watched_s: Mapped[int] = mapped_column(Integer, default=0)
    position_s: Mapped[int | None] = mapped_column(Integer)
    runtime_s: Mapped[int | None] = mapped_column(Integer)


class HomeReport(Base):
    """One snapshot of the home host (the ThinkCentre), pushed in by a reporter on it. A live view,
    pruned on a short schedule. Internet speed tests are kept separately: see SpeedTest."""

    __tablename__ = "home_reports"
    __table_args__ = (Index("ix_home_site_time", "site_id", "reported_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    cpu_temp_c: Mapped[float | None] = mapped_column(Float)
    load_1: Mapped[float | None] = mapped_column(Float)
    cpu_count: Mapped[int | None] = mapped_column(Integer)
    mem_total_mb: Mapped[int | None] = mapped_column(Integer)
    mem_available_mb: Mapped[int | None] = mapped_column(Integer)
    disk_total_mb: Mapped[int | None] = mapped_column(Integer)
    disk_free_mb: Mapped[int | None] = mapped_column(Integer)
    uptime_s: Mapped[int | None] = mapped_column(BigInteger)
    intake_c: Mapped[float | None] = mapped_column(Float)
    exhaust_c: Mapped[float | None] = mapped_column(Float)
    rx_bps: Mapped[float | None] = mapped_column(Float)
    tx_bps: Mapped[float | None] = mapped_column(Float)
    # Whether the reporter was told where the backups land. Without it, "no backup found" and
    # "backups are not being tracked" would look the same.
    backup_configured: Mapped[bool] = mapped_column(Boolean, default=False)
    backup_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SpeedTest(Base):
    """One internet speed test run on the home host. Kept for the trend line, unlike the live
    snapshots, and pruned after 90 days."""

    __tablename__ = "speed_tests"
    __table_args__ = (Index("ux_speed_site_time", "site_id", "tested_at", unique=True),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    tested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    down_mbps: Mapped[float] = mapped_column(Float)
    up_mbps: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[float | None] = mapped_column(Float)


class Presence(Base):
    """Who is home, as taps with an expiry. See hub/presence.py: on expiry, or with no record, the
    effective state is away, so a missed tap can never leave the system wrongly believing someone
    is home."""

    __tablename__ = "presence"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # home | away
    state: Mapped[str] = mapped_column(String(10))
    # nfc | geofence | manual
    source: Mapped[str] = mapped_column(String(20))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Notification(Base):
    """The alert outbox: one row per state change that has to reach the phone.

    It is written in the same transaction that records the change, so either both exist or neither
    does and a restart loses nothing. A sender loop (hub/outbox.py) takes due rows with SKIP LOCKED,
    sends them and retries with backoff. Delivery is at-least-once: a crash after the push is accepted
    but before the row is marked sent repeats the push, which is far better than a missed one.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        # a retried ingest must not write a second row for the same change
        UniqueConstraint("issue_id", "change", "changed_at", name="uq_notification_change"),
        Index("ix_notifications_due", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"))
    # new | regression | spike
    change: Mapped[str] = mapped_column(String(20))
    # When the change happened. For a spike it is the start of the window, so one window is one alert.
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    severity: Mapped[str] = mapped_column(String(4))

    # What to send, fixed when the row was written: the policy's answer then, and the words then.
    priority: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str | None] = mapped_column(String(10))
    site: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(300))

    # pending | sending | sent | failed. A failed row is shown in the console: an alert that could not
    # be sent must never look like an alert that was not needed.
    status: Mapped[str] = mapped_column(String(10), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When a sender took the row. A row left "sending" by a crash is picked up again once this is old.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # why the last attempt failed: a class name and a short reason, never the provider's token
    last_error: Mapped[str | None] = mapped_column(String(300))

    # Pushover's receipt for a priority 2 push, which it calls back about when it is acknowledged.
    receipt: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When Pushover reported the receipt expired with nobody acknowledging it: a P1 that did not get a
    # human. The console says so, because it means the phone did not wake anyone.
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Someone dealt with the incident (acknowledged, resolved, muted or snoozed it, or the hub resolved it by
    # itself) after this alert was sent. Written in the same transaction as that action. A repeating push for
    # it must stop, and a P1 that expired unanswered no longer needs anyone's attention.
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When Pushover confirmed it cancelled the repeating push. The sender does this separately and retries,
    # so a network failure never leaves a phone alarming for an incident that is already handled.
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    issue: Mapped["Issue"] = relationship()


class DailyStat(Base):
    """One site's numbers for one complete UTC day, written by hub/rollup.py before the raw rows
    behind them are pruned. A row exists only for a day when something was recorded, so a missing
    row means "no data", which is not the same as zero. Today is never here: it is computed live."""

    __tablename__ = "daily_stats"

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)

    checks_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    checks_ok: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # over successful checks only: a timeout has no meaningful latency
    latency_avg_ms: Mapped[int | None] = mapped_column(Integer)
    latency_p95_ms: Mapped[int | None] = mapped_column(Integer)

    events: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    issues_opened: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    alerts_sent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    alerts_failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    p1_expired: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

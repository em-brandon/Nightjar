from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Frame(BaseModel):
    module: str | None = None
    file: str | None = None
    function: str | None = None
    lineno: int | None = None
    context: str | None = None


class EventIn(BaseModel):
    exception_type: str = Field(max_length=200)
    message: str = Field(default="", max_length=4000)
    frames: list[Frame] = Field(default_factory=list, max_length=200)
    level: Literal["warning", "error", "fatal"] = "error"
    environment: str = "production"
    release: str | None = None
    request: dict[str, Any] | None = None
    extra: dict[str, Any] | None = None
    occurred_at: datetime | None = None


class IssueOut(BaseModel):
    id: int
    site: str
    env: str
    severity: str
    status: Literal["open", "resolved", "muted", "snoozed"]
    snoozed_until: datetime | None
    # someone has looked at it since it last opened
    acknowledged: bool
    title: str
    culprit: str
    fingerprint: str
    event_count: int
    first_seen: datetime
    last_seen: datetime


DayState = Literal["ok", "deg", "out", "gap"]


class SiteHealthOut(BaseModel):
    id: int
    name: str
    env: str
    # down: an open P1, or 2 failed probes in a row. warn: an open P2, one
    # failed probe, or a probe gone quiet. up: none. Derived, never stored.
    state: Literal["up", "warn", "down"]
    open_p1: int
    open_p2: int
    open_p3: int
    last_event: datetime | None

    # Uptime probe. Everything below is empty/None when the site has no url.
    monitored: bool
    probe_stale: bool
    last_check: datetime | None
    uptime_24h: float | None
    latency_ms: int | None
    latency_avg_ms: int | None
    # newest last; None marks a failed check
    latency_series: list[int | None]
    # 90 days, oldest first. gap = no checks that day (not "all good").
    ribbon: list[DayState]


class RisingOut(BaseModel):
    issue_id: int
    site: str
    title: str
    severity: str
    recent: int  # events in the last 30 min
    previous: int  # events in the 30 min before that
    series: list[int]  # six 10-minute buckets, oldest first


ActionName = Literal["ack", "resolve", "mute", "snooze", "reopen"]


class ActionIn(BaseModel):
    action: ActionName
    hours: int = Field(default=24, ge=1, le=720)  # snooze only


class LogEntry(BaseModel):
    at: datetime
    # system: derived from the issue itself. action: something a person did.
    kind: Literal["system", "action"]
    text: str


TicketKind = Literal["change", "report", "access"]


class TicketIn(BaseModel):
    kind: TicketKind
    subject: str = Field(min_length=1, max_length=300)
    sender: str = Field(default="", max_length=255)
    site: str | None = None  # site name
    due_at: datetime | None = None


class TicketPatch(BaseModel):
    status: Literal["open", "closed"]


class TicketOut(BaseModel):
    id: int
    kind: TicketKind
    subject: str
    sender: str
    site: str | None
    status: Literal["open", "closed"]
    created_at: datetime
    due_at: datetime | None
    closed_at: datetime | None
    overdue: bool


class DeployIn(BaseModel):
    ref: str = Field(default="", max_length=120)
    status: Literal["running", "success", "failed"]


class DeployOut(BaseModel):
    id: int
    site: str
    env: str
    ref: str
    status: Literal["running", "success", "failed"]
    created_at: datetime


ServiceState = Literal["running", "stopped", "unhealthy"]
StackState = Literal["running", "stopped", "degraded"]


class ServiceIn(BaseModel):
    name: str = Field(max_length=120)
    state: ServiceState
    mem_mb: int | None = Field(default=None, ge=0)
    restarts: int | None = Field(default=None, ge=0)
    # why it is in this state, e.g. "exited 137 (stopped or killed), 34 days ago"
    note: str | None = Field(default=None, max_length=160)


class StackReportIn(BaseModel):
    stack: str = Field(min_length=1, max_length=120)
    state: StackState
    mem_mb: int | None = Field(default=None, ge=0)
    mem_limit_mb: int | None = Field(default=None, ge=0)
    cpu_pct: float | None = Field(default=None, ge=0)
    host_mem_mb: int | None = Field(default=None, ge=0)
    disk_mb: int | None = Field(default=None, ge=0)
    restarts: int | None = Field(default=None, ge=0)
    host_disk_total_mb: int | None = Field(default=None, ge=0)
    host_disk_free_mb: int | None = Field(default=None, ge=0)
    services: list[ServiceIn] = Field(default_factory=list, max_length=60)


class StackAck(BaseModel):
    id: int


class StackOut(BaseModel):
    site: str
    env: str
    stack: str
    state: StackState
    # the reporter has stopped reporting: the numbers below are old, and a
    # silent reporter must never read as a healthy stack
    stale: bool
    reported_at: datetime
    mem_mb: int | None
    mem_limit_mb: int | None
    cpu_pct: float | None
    host_mem_mb: int | None
    disk_mb: int | None
    host_disk_total_mb: int | None
    host_disk_free_mb: int | None
    restarts: int | None
    # restarts in the last 10 minutes. Needs two reports in the window to show
    # anything: a single report reads 0, never a guess. None if not reported.
    restarts_10m: int | None
    # a container restarting again and again still reads as "running" between
    # crashes, so the loop is flagged on its own
    restart_loop: bool
    services: list[ServiceIn]
    mem_series: list[int | None]  # oldest first


class IngestOut(BaseModel):
    issue_id: int
    fingerprint: str
    event_count: int
    severity: str
    # what chunk 2's alert policy reads. None means "seen this, stay quiet".
    state_change: Literal["new", "regression", "spike"] | None

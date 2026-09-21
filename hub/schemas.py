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
    # How far the push has got, 0 to 100, if the pipeline can say (bytes uploaded, say). Optional:
    # without it the wall shows an indeterminate bar while the deploy is running.
    progress: int | None = Field(default=None, ge=0, le=100)


class DeployOut(BaseModel):
    id: int
    site: str
    env: str
    ref: str
    status: Literal["running", "success", "failed"]
    created_at: datetime
    progress: int | None = None


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


PlayMethod = Literal["direct_play", "direct_stream", "transcode"]


class MediaSessionIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=80)
    user: str = Field(min_length=1, max_length=120)
    item_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    series: str | None = Field(default=None, max_length=200)
    episode: str | None = Field(default=None, max_length=20)  # e.g. "S03E04"
    kind: str | None = Field(default=None, max_length=20)
    client: str | None = Field(default=None, max_length=120)
    device: str | None = Field(default=None, max_length=120)
    play_method: PlayMethod | None = None
    paused: bool = False
    position_s: int | None = Field(default=None, ge=0)
    runtime_s: int | None = Field(default=None, ge=0)


class MediaReportIn(BaseModel):
    up: bool
    version: str | None = Field(default=None, max_length=40)
    host_disk_total_mb: int | None = Field(default=None, ge=0)
    host_disk_free_mb: int | None = Field(default=None, ge=0)
    sessions: list[MediaSessionIn] = Field(default_factory=list, max_length=40)


class MediaAck(BaseModel):
    id: int


class MediaOut(BaseModel):
    site: str
    # the reporter has gone quiet: nothing below is current, and a silent reporter
    # must never read as an idle server
    stale: bool
    reported_at: datetime
    up: bool
    version: str | None
    host_disk_total_mb: int | None
    host_disk_free_mb: int | None
    # empty when stale, so an old snapshot cannot claim someone is still watching
    sessions: list[MediaSessionIn]


class MediaPlayOut(BaseModel):
    id: int
    user: str
    title: str
    series: str | None
    episode: str | None
    kind: str | None
    client: str | None
    device: str | None
    play_method: PlayMethod | None
    started_at: datetime
    last_seen_at: datetime
    watched_s: int
    runtime_s: int | None


class WeatherOut(BaseModel):
    # False when weather is off, has never been fetched, or the upstream is failing with no
    # earlier reading to show. Weather is decoration: this is never an error status.
    available: bool
    place: str
    stale: bool = False
    fetched_at: float | None = None
    temperature_c: float | None = None
    feels_like_c: float | None = None
    condition: str | None = None
    icon: str | None = None
    elevation_m: int | None = None
    # where the reading is for: the configured coordinates of the wall, not the weather grid's
    latitude: float | None = None
    longitude: float | None = None
    # False at night, so a clear sky can be a moon instead of a sun
    is_day: bool = True
    # the next six hours: the highest chance of rain, and how many hours until it reaches 50%
    # (0 means now). None when the forecast did not say.
    rain_chance_pct: int | None = None
    rain_in_h: int | None = None
    # UV index now, and today's peak
    uv_index: float | None = None
    uv_max: float | None = None
    # barometric pressure at sea level (what weather maps and barometers show), and the last three
    # hours' direction. The station's own pressure at this altitude is far lower, and is kept apart.
    pressure_hpa: int | None = None
    pressure_trend: str | None = None
    station_pressure_hpa: int | None = None


class HostIn(BaseModel):
    cpu_temp_c: float | None = Field(default=None, ge=-50, le=200)
    load_1: float | None = Field(default=None, ge=0)
    cpu_count: int | None = Field(default=None, ge=1)
    mem_total_mb: int | None = Field(default=None, ge=0)
    mem_available_mb: int | None = Field(default=None, ge=0)
    disk_total_mb: int | None = Field(default=None, ge=0)
    disk_free_mb: int | None = Field(default=None, ge=0)
    uptime_s: int | None = Field(default=None, ge=0)
    # the cabinet's own air sensors, when it has them: air going in, and air coming out
    intake_c: float | None = Field(default=None, ge=-50, le=200)
    exhaust_c: float | None = Field(default=None, ge=-50, le=200)
    # how fast data has been moving over the home connection, averaged over the last minute, in bits
    # per second. This is traffic, not the speed test: it says whether the line is busy right now.
    rx_bps: float | None = Field(default=None, ge=0)
    tx_bps: float | None = Field(default=None, ge=0)


class NetIn(BaseModel):
    down_mbps: float = Field(ge=0)
    up_mbps: float | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    tested_at: datetime


class HomeReportIn(BaseModel):
    host: HostIn = Field(default_factory=HostIn)
    # the last speed test, repeated on every report until the next one runs
    net: NetIn | None = None
    backup_configured: bool = False
    backup_ok_at: datetime | None = None


class HomeAck(BaseModel):
    id: int


class SpeedPoint(BaseModel):
    tested_at: datetime
    down_mbps: float
    up_mbps: float | None
    latency_ms: float | None


class HomeOut(BaseModel):
    site: str
    # the reporter has gone quiet: the numbers are old, and silence must never read as healthy
    stale: bool
    reported_at: datetime
    cpu_temp_c: float | None
    load_1: float | None
    cpu_count: int | None
    mem_total_mb: int | None
    mem_available_mb: int | None
    disk_total_mb: int | None
    disk_free_mb: int | None
    uptime_s: int | None
    intake_c: float | None
    exhaust_c: float | None
    rx_bps: float | None
    tx_bps: float | None
    backup_configured: bool
    backup_ok_at: datetime | None
    backup_age_h: float | None
    net: SpeedPoint | None
    speed_series: list[float]  # download Mbps of the last tests, oldest first


class PresenceIn(BaseModel):
    state: Literal["home", "away"]
    # an NFC tap sets home for 14 hours
    ttl_minutes: int = Field(default=840, ge=1, le=1440)
    source: Literal["nfc", "geofence", "manual"] = "nfc"
    token: str | None = Field(default=None, max_length=200)


class PresenceOut(BaseModel):
    # the EFFECTIVE state: away on expiry or with no record at all
    state: Literal["home", "away"]
    source: str | None
    expires_at: datetime | None
    set_at: datetime | None
    # False when nothing has ever been recorded
    recorded: bool


class RatePair(BaseModel):
    code: str  # USD, GBP, CNY
    rate: float  # Kenyan shillings for one unit of it


class RatesOut(BaseModel):
    available: bool
    base: str = "USD"
    quote: str = "KES"
    rate: float | None = None
    # what one US dollar, one pound and one yuan each cost in shillings, in the order the wall cycles
    pairs: list[RatePair] = []
    updated_unix: int | None = None
    stale: bool = False

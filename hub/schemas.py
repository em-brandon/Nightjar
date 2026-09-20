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
    title: str
    culprit: str
    fingerprint: str
    event_count: int
    first_seen: datetime
    last_seen: datetime


class SiteHealthOut(BaseModel):
    id: int
    name: str
    env: str
    # down: an open P1. warn: an open P2. up: neither. Derived, never stored.
    state: Literal["up", "warn", "down"]
    open_p1: int
    open_p2: int
    open_p3: int
    last_event: datetime | None


class IngestOut(BaseModel):
    issue_id: int
    fingerprint: str
    event_count: int
    severity: str
    # what chunk 2's alert policy reads. None means "seen this, stay quiet".
    state_change: Literal["new", "regression", "spike"] | None

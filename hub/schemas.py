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


class IngestOut(BaseModel):
    issue_id: int
    fingerprint: str
    event_count: int
    severity: str
    # what chunk 2's alert policy reads. None means "seen this, stay quiet".
    state_change: Literal["new", "regression", "spike"] | None

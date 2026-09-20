import secrets
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
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

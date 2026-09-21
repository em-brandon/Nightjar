"""Bring the database schema up to date. Replaces `create_all` on startup.

`create_all` only creates tables that are missing. It never alters one, so every
new column needed a hand-run ALTER TABLE, and a database could quietly drift from
the code. Migrations make each change explicit, ordered and repeatable.

To change the schema:   edit hub/models.py, then
    python -m alembic revision --autogenerate -m "what changed"
    (read the file it writes, then commit it)

`python -m alembic check` fails if the models and the migrations disagree, which
is how a forgotten migration gets caught before it becomes an outage.
"""

import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy import inspect, pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from .config import settings

log = logging.getLogger("nightjar.migrate")

ROOT = Path(__file__).resolve().parent.parent
BASELINE = "0001"


class SchemaMismatch(RuntimeError):
    """An existing database does not match what the code expects."""


def _config(url: str | None) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    if url:
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


async def _tables(url: str) -> set[str]:
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    finally:
        await engine.dispose()


async def _forget_stamp(url: str) -> None:
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    finally:
        await engine.dispose()


def upgrade(url: str | None = None) -> None:
    """Migrate to the latest schema. Safe to call on every start.

    Blocking, and it runs Alembic's own event loop, so call it from a thread
    (`asyncio.to_thread`) when inside async code.

    A database created by the old `create_all` has tables but no version record.
    That is adopted, not migrated over: it is stamped as the baseline, brought to
    head, and then CHECKED against the models. If it has drifted (a missing
    column, say) this raises and undoes the stamp, rather than declaring a wrong
    schema to be correct and failing later in production.
    """
    target = url or settings.database_url
    cfg = _config(url)

    tables = asyncio.run(_tables(target))
    legacy = "sites" in tables and "alembic_version" not in tables

    if legacy:
        log.warning("adopting an existing database created before migrations")
        command.stamp(cfg, BASELINE)
        command.upgrade(cfg, "head")
        try:
            command.check(cfg)
        except CommandError as exc:
            asyncio.run(_forget_stamp(target))
            raise SchemaMismatch(
                "This database predates migrations and does not match the current "
                "models, so it was not adopted. Fix the difference by hand (run "
                "`python -m alembic check` to see it), or start from a fresh database. "
                f"Detail: {exc}"
            ) from exc
        return

    command.upgrade(cfg, "head")


def check(url: str | None = None) -> None:
    """Raise if the database is not exactly what the models describe."""
    command.check(_config(url))

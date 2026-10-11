"""Migration tests. They run against real Postgres databases created for the test
and dropped afterwards, so they need the dev database container to be up:

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest discover -s tests -v

The point is to prove the things that hurt in production:
  * a fresh database reaches the schema the models describe
  * a database made by the OLD create_all is adopted without losing data
  * one that has drifted is REFUSED, not declared correct
  * a model changed without a migration is caught (`alembic check`)
"""

import asyncio
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, pool, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from hub import migrate  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import Base  # noqa: E402
from hub import models  # noqa: E402,F401


def _admin_url() -> str:
    return make_url(settings.database_url).set(database="postgres").render_as_string(hide_password=False)


async def _admin(sql: str) -> None:
    engine = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT", poolclass=pool.NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


async def _run(url: str, fn):
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.begin() as conn:
            return await fn(conn)
    finally:
        await engine.dispose()


def _reachable() -> bool:
    try:
        asyncio.run(_admin("SELECT 1"))
        return True
    except Exception:
        return False


# Tables created by a migration after the baseline. Add to this when a migration adds a table.
AFTER_BASELINE = {"media_reports", "media_plays", "home_reports", "speed_tests", "presence", "notifications"}
# Columns a later migration added to a table that already existed at the baseline. A database made by
# the old create_all cannot have them either. Add to this when a migration adds a column.
COLUMNS_AFTER_BASELINE = {"deploys": ["progress"], "sites": ["public_key", "allowed_origins"]}


def head_revision() -> str:
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(migrate._config(None)).get_current_head()


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class Migrations(unittest.TestCase):
    def setUp(self):
        self.name = f"nj_mig_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{self.name}"'))
        self.url = make_url(settings.database_url).set(database=self.name).render_as_string(hide_password=False)
        self.addCleanup(lambda: asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)')))

    def tables(self):
        async def go(conn):
            return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
        return asyncio.run(_run(self.url, go))

    def sql(self, statement):
        async def go(conn):
            result = await conn.execute(text(statement))
            return result.all() if result.returns_rows else None
        return asyncio.run(_run(self.url, go))

    def legacy_create_all(self):
        """What the hub used to do on startup, for the tables that existed then. Tables
        added by later migrations (the media ones) are left out: a database made by the
        old create_all cannot have them, and adopting one must still be able to add them."""
        legacy = [t for name, t in Base.metadata.tables.items() if name not in AFTER_BASELINE]

        async def go(conn):
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=legacy))
            for table, columns in COLUMNS_AFTER_BASELINE.items():
                for column in columns:
                    await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        asyncio.run(_run(self.url, go))

    # ------------------------------------------------------------------ fresh

    def test_fresh_database_reaches_the_models_schema(self):
        migrate.upgrade(self.url)
        self.assertEqual(self.tables() - {"alembic_version"}, set(Base.metadata.tables))
        self.assertEqual(self.sql("SELECT version_num FROM alembic_version"), [(head_revision(),)])
        migrate.check(self.url)  # models and migrations agree, so no error

    def test_upgrade_is_idempotent(self):
        migrate.upgrade(self.url)
        migrate.upgrade(self.url)
        self.assertEqual(len(self.sql("SELECT * FROM alembic_version")), 1)

    def test_downgrade_removes_everything(self):
        from alembic import command
        migrate.upgrade(self.url)
        command.downgrade(migrate._config(self.url), "base")
        self.assertEqual(self.tables() - {"alembic_version"}, set())

    # ---------------------------------------------------------------- legacy

    def test_a_create_all_database_is_adopted_and_keeps_its_data(self):
        self.legacy_create_all()
        self.assertNotIn("alembic_version", self.tables())
        self.sql("INSERT INTO clients (name) VALUES ('keep me')")

        migrate.upgrade(self.url)

        self.assertEqual(self.sql("SELECT name FROM clients"), [("keep me",)])
        self.assertEqual(self.sql("SELECT version_num FROM alembic_version"), [(head_revision(),)])
        migrate.check(self.url)

    def test_a_drifted_legacy_database_is_refused_and_not_stamped(self):
        # what the dev database looked like before the disk columns were added by hand
        self.legacy_create_all()
        self.sql("ALTER TABLE stack_reports DROP COLUMN disk_mb")

        with self.assertRaises(migrate.SchemaMismatch):
            migrate.upgrade(self.url)

        # It must NOT have recorded a version: a stamp on a wrong schema would make
        # the next start believe everything is fine.
        self.assertNotIn("alembic_version", self.tables())

    # ---------------------------------------------------------- drift guards

    def test_check_catches_a_model_changed_without_a_migration(self):
        from alembic.util.exc import CommandError
        migrate.upgrade(self.url)
        self.sql("ALTER TABLE sites ADD COLUMN forgotten text")  # stands in for a new model column
        with self.assertRaises(CommandError):
            migrate.check(self.url)

    def test_check_catches_a_dropped_column(self):
        from alembic.util.exc import CommandError
        migrate.upgrade(self.url)
        self.sql("ALTER TABLE stack_reports DROP COLUMN restarts")
        with self.assertRaises(CommandError):
            migrate.check(self.url)


if __name__ == "__main__":
    unittest.main()

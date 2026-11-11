"""Explicit Arena schema migrations.

Migrations are NEVER run from load(), so gunicorn workers never run DDL.
Operators run them with ``flask arena migrate`` (see cli.py).

Upgrade reuses CTFd's native plugin migration runner, so the revision is
stored in the native ``config`` table under ``arena_alembic_version``. The
native helper has no downgrade, so one is implemented here with the same
storage convention.
"""

import os

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from flask import current_app
from sqlalchemy import create_engine, inspect, pool

from CTFd.plugins import migrations as native_migrations
from CTFd.utils import set_config

PLUGIN_NAME = "arena"
VERSION_KEY = PLUGIN_NAME + "_alembic_version"
MIGRATIONS_PATH = os.path.join(os.path.dirname(__file__), "migrations")
ARENA_TABLES = ("arena_events", "arena_registrations", "arena_roles")


class MigrationError(RuntimeError):
    pass


def _script():
    config = Config()
    config.set_main_option("script_location", MIGRATIONS_PATH)
    config.set_main_option("version_locations", MIGRATIONS_PATH)
    return ScriptDirectory.from_config(config)


def head_revision():
    return _script().get_current_head()


def is_sqlite(app=None):
    app = app or current_app
    return app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite")


def current_revision():
    """Revision recorded in the native config table (None if never migrated)."""
    return native_migrations.current(plugin_name=PLUGIN_NAME)


def existing_arena_tables():
    engine = current_app.db.engine
    names = set(inspect(engine).get_table_names())
    return sorted(t for t in ARENA_TABLES if t in names)


def upgrade(revision=None):
    if is_sqlite():
        # Same behaviour as CTFd core/native plugins on SQLite (dev/tests only).
        current_app.db.create_all()
        return head_revision()
    native_migrations.upgrade(plugin_name=PLUGIN_NAME, revision=revision)
    return current_revision()


def downgrade(target):
    """Downgrade to ``target`` revision, or ``"base"`` to drop all Arena tables."""
    if target in (None, ""):
        raise MigrationError("A target revision (or 'base') is required")
    target = None if target == "base" else target

    script = _script()
    if target is not None:
        script.get_revision(target)  # raises if unknown

    current = current_revision()
    if is_sqlite() and current is None and existing_arena_tables():
        # SQLite schema is created with create_all() and is untracked.
        current = head_revision()
    if current is None:
        return None
    if current == target:
        return current

    revs = list(script.iterate_revisions(upper=current, lower=target))
    if is_sqlite():
        # In-memory SQLite: a new engine would be a different, empty database.
        engine = current_app.db.engine
        dispose = False
    else:
        url = current_app.config["SQLALCHEMY_DATABASE_URI"]
        engine = create_engine(url, poolclass=pool.NullPool)
        dispose = True
    conn = engine.connect()
    try:
        context = MigrationContext.configure(conn)
        op = Operations(context)
        for rev in revs:  # newest first
            with context.begin_transaction():
                rev.module.downgrade(op=op)
            set_config(VERSION_KEY, rev.down_revision)
    finally:
        conn.close()
        if dispose:
            engine.dispose()
    set_config(VERSION_KEY, target)
    return target


def schema_state():
    """Return (ok: bool, detail: dict) without raising."""
    head = head_revision()
    try:
        if is_sqlite():
            tables = existing_arena_tables()
            ok = len(tables) == len(ARENA_TABLES)
            return ok, {"backend": "sqlite", "head": head, "tables": tables}
        current = current_revision()
        return current == head, {"backend": "sql", "head": head, "current": current}
    except Exception as e:  # noqa: BLE001 - health check must never raise
        return False, {"head": head, "error": type(e).__name__}

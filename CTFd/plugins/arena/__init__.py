"""Arena: multi-event extension for CTFd (Phase 1 - foundation).

load() is called by CTFd's init_plugins() inside create_app(). It:
  * reads and validates Arena settings,
  * imports Arena models,
  * registers the ``flask arena`` CLI and health endpoints.

It never runs schema migrations on MySQL/MariaDB/PostgreSQL. Gunicorn workers
therefore never execute DDL; operators run ``flask arena migrate`` explicitly.
On SQLite (development/tests only) it calls db.create_all(), the same
behaviour CTFd core and the bundled dynamic_challenges plugin already use.
"""

from CTFd.plugins.arena import models  # noqa: F401  (register tables)
from CTFd.plugins.arena.cli import arena_cli
from CTFd.plugins.arena.config import ArenaSettings
from CTFd.plugins.arena.core import schema as schema_guard

ARENA_VERSION = "0.1.0-phase1"


def load(app):
    from CTFd.plugins.arena import dbmigrate
    from CTFd.plugins.arena.health import health

    settings = ArenaSettings.from_app(app)
    app.extensions["arena"] = {"settings": settings, "version": ARENA_VERSION}

    # The CLI is always available so operators can migrate before enabling.
    if "arena" not in app.cli.commands:
        app.cli.add_command(arena_cli)

    if not settings.enabled:
        app.logger.info("Arena is disabled (ARENA_ENABLED=false)")
        return

    if dbmigrate.is_sqlite(app):
        app.db.create_all()
    else:
        ok, detail = dbmigrate.schema_state()
        if not ok:
            app.logger.warning(
                "Arena schema is not at head (%s). Arena routes will return 503 "
                "until an operator runs: flask arena migrate",
                detail,
            )

    schema_guard.reset_cache()
    app.register_blueprint(health)

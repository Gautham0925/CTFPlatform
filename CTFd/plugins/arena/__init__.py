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
from CTFd.plugins.arena.config import ArenaSettings, import_environment
from CTFd.plugins.arena.core import schema as schema_guard

ARENA_VERSION = "0.2.0-dev"


def load(app):
    from CTFd.plugins.arena import dbmigrate
    from CTFd.plugins.arena.health import health, install_priority_probe

    import_environment(app)
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
    install_priority_probe(app)
    for bp in _feature_blueprints(settings):
        app.register_blueprint(bp)
    _install_auth_observers(app)
    _install_ui(app)


def _install_ui(app):
    from CTFd.plugins import (
        register_admin_plugin_menu_bar,
        register_plugin_assets_directory,
        register_user_page_menu_bar,
    )
    from CTFd.plugins.arena.ui import ui

    app.register_blueprint(ui)
    register_plugin_assets_directory(app, base_path="/plugins/arena/assets/")
    register_user_page_menu_bar("Competitions", "arena")  # menu adds the leading /
    register_admin_plugin_menu_bar("Arena", "/arena/admin")


def _install_auth_observers(app):
    """Record native login/logout as Arena activity without touching core:
    observe the native auth endpoints' request/response."""
    from flask import g, session

    from CTFd.plugins.arena.services import activity

    @app.before_request
    def _arena_capture_logout():
        if request_endpoint() == "auth.logout":
            g.arena_logout_user = session.get("id")

    @app.after_request
    def _arena_auth_activity(response):
        endpoint = request_endpoint()
        if endpoint == "auth.login" and request_method() == "POST":
            uid = session.get("id")
            if uid and response.status_code in (200, 302):
                activity.record("auth.login", user_id=uid)
        elif endpoint == "auth.logout":
            uid = getattr(g, "arena_logout_user", None)
            if uid:
                activity.record("auth.logout", user_id=uid)
        return response


def request_endpoint():
    from flask import request

    return request.endpoint


def request_method():
    from flask import request

    return request.method


def _feature_blueprints(settings):
    from CTFd.plugins.arena import tasks  # noqa: F401 (registers job handlers)
    from CTFd.plugins.arena.api.ad import ad_api
    from CTFd.plugins.arena.api.admin_challenges import admin_challenges_api
    from CTFd.plugins.arena.api.admin_events import admin_events_api
    from CTFd.plugins.arena.api.admin_platform import platform_api
    from CTFd.plugins.arena.api.analytics import analytics_api
    from CTFd.plugins.arena.api.challenges import challenges_api
    from CTFd.plugins.arena.api.events import events_api
    from CTFd.plugins.arena.api.instances import instances_api
    from CTFd.plugins.arena.api.labs import labs_api
    from CTFd.plugins.arena.api.scoreboard import scoreboard_api
    from CTFd.plugins.arena.api.teams import teams_api

    _register_hooks()
    return [
        events_api,
        admin_events_api,
        teams_api,
        challenges_api,
        admin_challenges_api,
        scoreboard_api,
        platform_api,
        analytics_api,
        instances_api,
        ad_api,
        labs_api,
    ] + _optional_blueprints(settings)


def _optional_blueprints(settings):
    out = []
    if settings.certificates_enabled:  # disabled by default: no routes at all
        from CTFd.plugins.arena.api.certificates import certificates_api

        out.append(certificates_api)
    return out


def _register_hooks():
    """Wire cross-service hooks once (idempotent)."""
    from CTFd.plugins.arena.services import (
        challenges,
        events,
        registration,
        scoreboard,
        scoring,
        submissions,
        teams,
    )

    def add(hooks, fn):
        if fn not in hooks:
            hooks.append(fn)

    add(registration.DEACTIVATE_HOOKS, teams.on_registration_deactivated)
    add(events.DELETE_HOOKS, challenges.delete_all_for_event)
    add(teams.HAS_HISTORY_HOOKS, _team_has_solves)
    add(submissions.SOLVE_TXN_HOOKS, scoring.on_solve)
    add(submissions.POST_COMMIT_HOOKS, _invalidate_scoreboard)
    add(registration.DEACTIVATE_HOOKS, scoreboard.invalidate)


def _invalidate_scoreboard(event, ec, entity, result):
    from CTFd.plugins.arena.services import scoreboard

    if result.get("status") in ("correct", "hint_unlocked"):
        scoreboard.invalidate(event.id)


def _team_has_solves(team):
    from CTFd.plugins.arena.models import ArenaSolve, ArenaSubmission
    from CTFd.plugins.arena.services.entity import team_key

    key = team_key(team.id)
    return bool(
        ArenaSolve.query.filter_by(event_id=team.event_id, entity_key=key).first()
        or ArenaSubmission.query.filter_by(
            event_id=team.event_id, entity_key=key
        ).first()
    )

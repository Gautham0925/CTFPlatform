"""Schema-readiness guard.

Because migrations are not run at startup, a worker can start against a
database whose Arena schema is missing or behind. Arena routes then answer
503 instead of failing with SQL errors. The check is cached in-process for
ARENA_SCHEMA_CHECK_TTL seconds so it costs one config read per TTL per
worker.
"""

import time

from flask import Blueprint, current_app, jsonify

from CTFd.plugins.arena import dbmigrate

_state = {"checked_at": 0.0, "ok": False}


def reset_cache():
    _state["checked_at"] = 0.0
    _state["ok"] = False


def schema_ready():
    ttl = current_app.extensions["arena"]["settings"].schema_check_ttl
    now = time.monotonic()
    if ttl and _state["ok"] and now - _state["checked_at"] < ttl:
        return True
    ok, _ = dbmigrate.schema_state()
    _state["ok"] = ok
    _state["checked_at"] = now
    return ok


def _guard():
    if not schema_ready():
        response = jsonify(
            {
                "success": False,
                "errors": [
                    "Arena database schema is not up to date. "
                    "An operator must run: flask arena migrate"
                ],
            }
        )
        response.status_code = 503
        return response
    return None


def arena_blueprint(name, import_name, **kwargs):
    """Blueprint factory: every Arena feature blueprint gets the schema guard."""
    bp = Blueprint(name, import_name, **kwargs)
    bp.before_request(_guard)
    return bp

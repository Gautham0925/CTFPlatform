"""Liveness and readiness endpoints.

/arena/healthz : process is up, no I/O (for container liveness probes).
/arena/readyz  : DB, cache and Arena schema checks. Returns only booleans so
                 it is safe to expose; detailed status is admin-only (Phase 14).
"""

import time
import uuid

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from CTFd.cache import cache
from CTFd.models import db
from CTFd.plugins.arena import dbmigrate

health = Blueprint("arena_health", __name__)


def check_db():
    try:
        db.session.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return False


def check_cache():
    key = "arena:readyz:" + uuid.uuid4().hex
    try:
        cache.set(key, 1, timeout=10)
        ok = cache.get(key) == 1
        cache.delete(key)
        return ok
    except Exception:  # noqa: BLE001 - Redis down must not raise
        return False


@health.route("/arena/healthz")
def healthz():
    return jsonify({"status": "ok", "time": int(time.time())})


@health.route("/arena/readyz")
def readyz():
    db_ok = check_db()
    cache_ok = check_cache()
    schema_ok = dbmigrate.schema_state()[0] if db_ok else False
    ok = db_ok and cache_ok and schema_ok
    response = jsonify(
        {"ready": ok, "checks": {"db": db_ok, "cache": cache_ok, "schema": schema_ok}}
    )
    response.status_code = 200 if ok else 503
    response.headers["Cache-Control"] = "no-store"
    return response


_PROBES = {"arena_health.healthz": healthz, "arena_health.readyz": readyz}


def install_priority_probe(app):
    """Answer probes before every other before_request hook.

    Native hooks (setup redirect, import lock, user tracking, ...) would
    otherwise turn /arena/readyz into a 302 to /setup on a fresh install,
    which a redirect-following probe reports as healthy.
    """

    def _probe_first():
        view = _PROBES.get(request.endpoint)
        if view is not None:
            return view()
        return None

    app.before_request_funcs.setdefault(None, []).insert(0, _probe_first)

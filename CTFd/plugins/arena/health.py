"""Liveness and readiness endpoints.

/arena/healthz : process is up, no I/O (for container liveness probes).
/arena/readyz  : DB, cache and Arena schema checks. Returns only booleans so
                 it is safe to expose; detailed status is admin-only (Phase 14).
"""

import time
import uuid

from flask import Blueprint, jsonify
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

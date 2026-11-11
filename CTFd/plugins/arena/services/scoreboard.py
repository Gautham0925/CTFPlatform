"""Cached scoreboard with ETag support and last-known-good fallback.

Keys (Arena cache, see arena_cache):
  arena:sbver:<event>              integer version, bumped on every score change
  arena:sb:<event>:<scope>:<ver>   cached JSON payload (TTL 300 s)
  arena:sblock:<event>:<scope>     stampede guard while recomputing

Request path:
  * ETag = W/"<event>-<scope>-<version>"; a matching If-None-Match → 304
    with one cache GET and no DB work.
  * cache miss → one process recomputes (guarded); others serve the previous
    payload if available.
  * Redis down → in-process last-known-good copy, else the DB snapshot table,
    else compute directly from the DB (source of truth).
"""

import datetime
import logging
import threading

from CTFd.models import db
from CTFd.plugins.arena import arena_cache
from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.models import ArenaScoreboardSnapshot
from CTFd.plugins.arena.services import scoring
from CTFd.plugins.arena.services import settings as event_settings

log = logging.getLogger("arena.scoreboard")
PAYLOAD_TTL = 300
SCOPES = ("public", "admin", "public:users", "admin:users")

_lkg = {}  # (event_id, scope) -> payload ; bounded by number of events*scopes
_lkg_lock = threading.Lock()


def _vkey(event_id):
    return f"arena:sbver:{event_id}"


def version(event_id):
    ver = arena_cache.get_int(_vkey(event_id))
    return ver if ver is not None else 0


def invalidate(event, *_args, **_kwargs):
    """Bump the event's scoreboard version (call after any score change).
    Accepts an event or an event id (usable directly as a service hook)."""
    arena_cache.incr(_vkey(getattr(event, "id", event)))


def etag(event_id, scope, ver):
    return f'W/"arena-{event_id}-{scope}-{ver}"'


def _build(event, scope):
    by = "user" if scope.endswith(":users") else "entity"
    base_scope = scope.split(":", 1)[0]
    rows = scoring.compute_standings(event, scope=base_scope, by=by)
    return {
        "event_id": event.id,
        "scope": scope,
        "mode": "individual"
        if by == "user" or event.team_mode == "individual"
        else "team",
        "frozen": bool(
            event.freeze_at
            and base_scope != "admin"
            and datetime.datetime.utcnow() >= event.freeze_at
        ),
        "freeze_at": v.iso(event.freeze_at),
        "refresh_seconds": event_settings.get(event, "scoreboard_refresh_seconds"),
        "generated_at": v.iso(datetime.datetime.utcnow()),
        "standings": rows,
    }


def _remember(event_id, scope, payload, ver):
    with _lkg_lock:
        _lkg[(event_id, scope)] = (ver, payload)


def save_snapshot(event, scope="public"):
    """Persist a last-known-good snapshot (worker job, Phase 6)."""
    payload = _build(event, scope)
    ver = version(event.id)
    row = db.session.get(ArenaScoreboardSnapshot, (event.id, scope))
    if row is None:
        row = ArenaScoreboardSnapshot(event_id=event.id, scope=scope)
        db.session.add(row)
    row.version, row.payload, row.computed_at = ver, payload, datetime.datetime.utcnow()
    db.session.commit()
    return row


def get(event, scope="public"):
    """Return (payload, etag, source)."""
    ver = version(event.id)
    cache_ok = arena_cache.available()
    key = f"arena:sb:{event.id}:{scope}:{ver}"
    if cache_ok:
        cached = arena_cache.get_json(key)
        if cached is not None:
            _remember(event.id, scope, cached, ver)
            return cached, etag(event.id, scope, ver), "cache"
        guard = arena_cache.add(f"arena:sblock:{event.id}:{scope}", 1, ttl=5)
        if guard is False:
            with _lkg_lock:
                prev = _lkg.get((event.id, scope))
            if prev is not None:
                return prev[1], etag(event.id, scope, prev[0]), "stale"
    if not arena_cache.available():
        # Redis down: serve the in-process copy if it is recent enough, else
        # compute from the DB (cheap at the target scale), else DB snapshot.
        with _lkg_lock:
            prev = _lkg.get((event.id, scope))
        try:
            payload = _build(event, scope)
            _remember(event.id, scope, payload, -1)
            return payload, None, "db"
        except Exception:  # noqa: BLE001 - DB trouble too: degrade further
            db.session.rollback()
            log.exception("scoreboard compute failed during cache outage")
            if prev is not None:
                return prev[1], None, "lkg"
            snap = db.session.get(ArenaScoreboardSnapshot, (event.id, scope))
            if snap is not None:
                return snap.payload, None, "snapshot"
            raise
    payload = _build(event, scope)
    arena_cache.set_json(key, payload, PAYLOAD_TTL)
    arena_cache.delete(f"arena:sblock:{event.id}:{scope}")
    _remember(event.id, scope, payload, ver)
    return payload, etag(event.id, scope, ver), "computed"


def clear_local():
    with _lkg_lock:
        _lkg.clear()

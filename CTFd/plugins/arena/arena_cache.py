"""Arena's own small cache layer with short timeouts and a circuit breaker.

Why not reuse CTFd's Flask-Caching client directly: CTFd's Redis client has
no socket timeouts (a hung Redis would block requests indefinitely) and is
shared with native sessions. Arena therefore opens its *own* client with
``socket_timeout``/``socket_connect_timeout`` = ARENA_REDIS_TIMEOUT (default
0.25 s) and stops trying for ARENA_REDIS_BREAKER_SECONDS (default 5 s) after a
failure. Without Redis (development/tests) it uses Flask-Caching's backend.

All operations return None/False on failure instead of raising.
"""

import json
import logging
import threading
import time

from flask import current_app

log = logging.getLogger("arena.cache")

_lock = threading.Lock()
_state = {"client": None, "url": None, "open_until": 0.0, "failures": 0}


def _redis_url(app):
    if app.config.get("CACHE_TYPE") != "redis":
        return None
    return app.config.get("CACHE_REDIS_URL")


def _client():
    app = current_app
    url = _redis_url(app)
    if url is None:
        return None
    with _lock:
        if _state["client"] is None or _state["url"] != url:
            import redis

            timeout = float(app.config.get("ARENA_REDIS_TIMEOUT", 0.25))
            _state["client"] = redis.Redis.from_url(
                url, socket_timeout=timeout, socket_connect_timeout=timeout
            )
            _state["url"] = url
        return _state["client"]


def available():
    return time.monotonic() >= _state["open_until"]


def _trip(exc):
    seconds = float(current_app.config.get("ARENA_REDIS_BREAKER_SECONDS", 5))
    _state["open_until"] = time.monotonic() + seconds
    _state["failures"] += 1
    log.warning(
        "arena cache unavailable (%s); bypassing for %ss", type(exc).__name__, seconds
    )


def stats():
    return {"failures": _state["failures"], "breaker_open": not available()}


def reset_breaker():
    _state["open_until"] = 0.0


def _fallback():
    from CTFd.cache import cache

    return cache


def _call(redis_fn, fallback_fn, default=None):
    if not available():
        return default
    client = _client()
    try:
        if client is not None:
            return redis_fn(client)
        return fallback_fn(_fallback())
    except Exception as e:  # noqa: BLE001 - cache must never break requests
        _trip(e)
        return default


def get_json(key):
    raw = _call(lambda c: c.get(key), lambda c: c.get(key))
    if raw is None:
        return None
    if isinstance(raw, (bytes, str)):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw


def set_json(key, value, ttl):
    payload = json.dumps(value, separators=(",", ":"))
    return bool(
        _call(
            lambda c: c.set(key, payload, ex=ttl),
            lambda c: c.set(key, payload, timeout=ttl),
            default=False,
        )
    )


def get_int(key):
    raw = _call(lambda c: c.get(key), lambda c: c.get(key))
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def incr(key):
    def fb(c):
        value = (c.get(key) or 0) + 1
        c.set(key, value, timeout=0)
        return value

    return _call(lambda c: c.incr(key), fb)


def add(key, value, ttl):
    """SET NX. Returns True if set, False if exists, None if cache is down."""
    if not available():
        return None
    return _call(
        lambda c: bool(c.set(key, value, nx=True, ex=ttl)),
        lambda c: bool(c.add(key, value, timeout=ttl)),
        default=None,
    )


def delete(key):
    return _call(lambda c: c.delete(key), lambda c: c.delete(key))


def has_redis():
    try:
        return _redis_url(current_app) is not None
    except RuntimeError:
        return False


def rpush_capped(key, value, cap):
    """Append to a Redis list and keep only the newest `cap` items.
    Returns (ok, dropped) ; ok=False when Redis is unavailable."""
    if not available() or not has_redis():
        return False, 0
    try:
        client = _client()
        pipe = client.pipeline(transaction=True)
        pipe.rpush(key, json.dumps(value, separators=(",", ":")))
        pipe.llen(key)
        pipe.ltrim(key, -cap, -1)
        _n, length, _ok = pipe.execute()
        return True, max(0, int(length) - cap)
    except Exception as e:  # noqa: BLE001
        _trip(e)
        return False, 0


def peek_batch(key, n):
    if not available() or not has_redis():
        return []
    try:
        raw = _client().lrange(key, 0, n - 1)
        return [json.loads(x) for x in raw]
    except Exception as e:  # noqa: BLE001
        _trip(e)
        return []


def trim_front(key, n):
    """Drop the first n items (after they were persisted)."""
    if not has_redis():
        return False
    try:
        _client().ltrim(key, n, -1)
        return True
    except Exception as e:  # noqa: BLE001
        _trip(e)
        return False


def llen(key):
    if not has_redis() or not available():
        return None
    try:
        return int(_client().llen(key))
    except Exception as e:  # noqa: BLE001
        _trip(e)
        return None

"""Per-account rate limiting for Arena endpoints.

CTFd's native ``ratelimit`` keys on the client IP. That is right for login
and registration, but wrong for actions taken by signed-in participants: at a
venue where hundreds of players share one NAT address, an IP-keyed limit of
30 flag submissions/minute would apply to the whole room. Arena keys on the
account instead and falls back to the IP only for anonymous requests. Coarse
per-IP flood protection stays at the edge (nginx).

Honours CTFd's ``RATELIMIT_DISABLE`` and uses the same cache backend.
"""

import functools

from flask import current_app, request, session

from CTFd.cache import cache
from CTFd.utils.user import get_ip


def ratelimit(method="POST", limit=50, interval=300, key_prefix="arena-rl"):
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            if request.method != method or current_app.config.get("RATELIMIT_DISABLE"):
                return f(*args, **kwargs)
            uid = session.get("id")
            who = f"u{int(uid)}" if uid else f"ip{get_ip()}"
            key = f"{key_prefix}:{who}:{request.endpoint}"
            current = cache.get(key)
            if current is not None and int(current) >= limit:
                from CTFd.plugins.arena.api import ApiError

                raise ApiError(
                    429,
                    f"Too many requests. Limit is {limit} requests in {interval} seconds",
                    code="ratelimited",
                )
            if current is None:
                cache.set(key, 1, timeout=interval)
            else:
                cache.set(key, int(current) + 1, timeout=interval)
            return f(*args, **kwargs)

        return wrapper

    return decorator

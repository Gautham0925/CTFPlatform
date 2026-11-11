"""Synchronous audit trail for administrative actions.

Administrative actions are low-volume and must be reliably recorded, so they
are written synchronously in their own transaction after the action commits.
High-volume participant activity uses services.activity (buffered, Phase 7).
Sensitive keys are stripped from metadata before storage.
"""

import logging

from CTFd.models import db
from CTFd.plugins.arena.models import ArenaActivity

log = logging.getLogger("arena.audit")

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "token",
        "secret",
        "flag",
        "submission",
        "provided",
        "code",
        "invite_code",
        "join_code",
        "credentials",
        "api_key",
        "session",
        "nonce",
    }
)
MAX_META_ITEMS = 32


def scrub(meta):
    if not isinstance(meta, dict):
        return None
    out = {}
    for key, value in list(meta.items())[:MAX_META_ITEMS]:
        key = str(key)[:48]
        if key.lower() in SENSITIVE_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value if not isinstance(value, str) else value[:256]
        elif isinstance(value, (list, tuple)):
            out[key] = [str(x)[:64] for x in value[:32]]
        elif isinstance(value, dict):
            out[key] = scrub(value)
    return out


def audit(action, actor=None, event_id=None, target=None, meta=None, actor_type=None):
    from CTFd.utils.user import authed, get_current_user

    try:
        if actor is None and authed():
            actor = get_current_user()
        row = ArenaActivity(
            event_id=event_id,
            actor_user_id=actor.id if actor is not None else None,
            actor_type=actor_type or ("admin" if actor is not None else "system"),
            action=action[:48],
            target_type=str(target[0])[:32] if target else None,
            target_id=str(target[1])[:64] if target else None,
            meta=scrub(meta),
        )
        db.session.add(row)
        db.session.commit()
        return row
    except Exception:  # noqa: BLE001 - the action already committed
        db.session.rollback()
        log.exception("failed to write audit record for %s", action)
        return None

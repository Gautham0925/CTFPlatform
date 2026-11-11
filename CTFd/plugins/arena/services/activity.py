"""Participant activity tracking (Phase 7).

record() never raises and never blocks on the database when Redis is
available: events are RPUSHed to a capped Redis list and batch-inserted by
the worker job ``activity.flush`` (at-least-once; a crash between insert and
trim can duplicate a batch, never lose one already acknowledged). Without
Redis (development/tests) records are inserted synchronously.

Privacy: no IP addresses, user agents, flags or free-text submissions are
recorded. Metadata is scrubbed with services.audit.scrub.
"""

import datetime
import logging

from flask import current_app

from CTFd.models import db
from CTFd.plugins.arena import arena_cache
from CTFd.plugins.arena.models import ArenaActivity
from CTFd.plugins.arena.services.audit import scrub

log = logging.getLogger("arena.activity")
QUEUE_KEY = "arena:activity"
_counters = {"buffered": 0, "direct": 0, "dropped": 0, "failed": 0}

PARTICIPANT_ACTIONS = frozenset(
    {
        "auth.login",
        "auth.logout",
        "registration.register",
        "registration.withdraw",
        "registration.join_code",
        "event.view",
        "team.create",
        "team.join",
        "team.leave",
        "team.kick",
        "team.captain_transfer",
        "team.invite",
        "team.invite_accept",
        "team.invite_reject",
        "challenge.view",
        "challenge.attempt",
        "challenge.solve",
        "hint.unlock",
        "lab.allocate",
        "lab.release",
        "lab.access",
        "instance.allocate",
        "instance.release",
        "ad.flag_submit",
        "ad.round_participation",
    }
)


def _cap():
    return int(current_app.config.get("ARENA_ACTIVITY_QUEUE_CAP", 100000))


def counters():
    return dict(_counters, queue_length=arena_cache.llen(QUEUE_KEY))


def record(action, user_id=None, event_id=None, target=None, meta=None):
    """Record participant activity. Call only after your transaction committed."""
    try:
        item = {
            "event_id": event_id,
            "actor_user_id": user_id,
            "actor_type": "user",
            "action": str(action)[:48],
            "target_type": str(target[0])[:32] if target else None,
            "target_id": str(target[1])[:64]
            if target and target[1] is not None
            else None,
            "meta": scrub(meta),
            "created_at": datetime.datetime.utcnow().isoformat(),
        }
        if arena_cache.has_redis():
            ok, dropped = arena_cache.rpush_capped(QUEUE_KEY, item, _cap())
            if ok:
                _counters["buffered"] += 1
                _counters["dropped"] += dropped
                return True
            # Redis unavailable: drop rather than add DB load on the hot path.
            _counters["dropped"] += 1
            return False
        _insert([item], own_session=True)
        _counters["direct"] += 1
        return True
    except Exception:  # noqa: BLE001 - activity must never break a request
        _counters["failed"] += 1
        log.exception("activity record failed for %s", action)
        return False


def _insert(items, own_session=False):
    rows = []
    for it in items:
        created = it.get("created_at")
        try:
            created = datetime.datetime.fromisoformat(created) if created else None
        except (TypeError, ValueError):
            created = None
        rows.append(
            {
                "event_id": it.get("event_id"),
                "actor_user_id": it.get("actor_user_id"),
                "actor_type": it.get("actor_type") or "user",
                "action": (it.get("action") or "unknown")[:48],
                "target_type": it.get("target_type"),
                "target_id": it.get("target_id"),
                "meta": it.get("meta"),
                "created_at": created or datetime.datetime.utcnow(),
            }
        )
    if not rows:
        return 0
    # Contract: record() is only called AFTER the caller's transaction has
    # committed, so committing here cannot publish the caller's changes.
    try:
        db.session.execute(ArenaActivity.__table__.insert(), rows)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return len(rows)


def flush(batch=500, max_batches=20):
    """Move buffered activity from Redis into the database (worker job)."""
    total = 0
    for _ in range(max_batches):
        items = arena_cache.peek_batch(QUEUE_KEY, batch)
        if not items:
            break
        # Events may have been deleted meanwhile: null their event_id.
        from CTFd.plugins.arena.models import ArenaEvent

        ids = {i.get("event_id") for i in items if i.get("event_id")}
        existing = {
            e
            for (e,) in db.session.query(ArenaEvent.id).filter(
                ArenaEvent.id.in_(ids or [0])
            )
        }
        for it in items:
            if it.get("event_id") and it["event_id"] not in existing:
                it["event_id"] = None
        _insert(items)
        arena_cache.trim_front(QUEUE_KEY, len(items))
        total += len(items)
        if len(items) < batch:
            break
    return total


def prune(now=None, batch=5000):
    """Retention: participant activity ARENA_ACTIVITY_RETENTION_DAYS (180),
    admin/system audit ARENA_AUDIT_RETENTION_DAYS (730)."""
    now = now or datetime.datetime.utcnow()
    cfg = current_app.config
    total = 0
    for types, days in (
        (("user",), int(cfg.get("ARENA_ACTIVITY_RETENTION_DAYS", 180))),
        (
            ("admin", "system", "runner"),
            int(cfg.get("ARENA_AUDIT_RETENTION_DAYS", 730)),
        ),
    ):
        cutoff = now - datetime.timedelta(days=days)
        while True:
            ids = [
                i
                for (i,) in db.session.query(ArenaActivity.id)
                .filter(
                    ArenaActivity.actor_type.in_(types),
                    ArenaActivity.created_at < cutoff,
                )
                .limit(batch)
            ]
            if not ids:
                break
            ArenaActivity.query.filter(ArenaActivity.id.in_(ids)).delete(
                synchronize_session=False
            )
            db.session.commit()
            total += len(ids)
    return total


def serialize(row, include_actor=True):
    from CTFd.plugins.arena.validation import iso

    data = {
        "id": row.id,
        "event_id": row.event_id,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "meta": row.meta,
        "created_at": iso(row.created_at),
    }
    if include_actor:
        data.update(actor_user_id=row.actor_user_id, actor_type=row.actor_type)
    return data

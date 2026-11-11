"""External lab sessions (Phase 11).

Provider calls never run inside participant requests: sessions are provisioned,
reset and released by worker jobs; completion is learned only by the
server-side ``labs.sync`` job polling the provider (client claims are never
accepted). Provider failures mark sessions failed / retry jobs; they never
propagate to CTFd requests.
"""

import datetime
import uuid

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from CTFd.models import db
from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.api import ApiError
from CTFd.plugins.arena.labs import make_provider
from CTFd.plugins.arena.labs.base import ProviderError
from CTFd.plugins.arena.labs.crypto import decrypt, encrypt
from CTFd.plugins.arena.labs.ssrf import SSRFError, check_url
from CTFd.plugins.arena.models import (
    ACTIVE_LAB_STATUSES,
    LAB_ADAPTERS,
    ArenaEventLab,
    ArenaLabProvider,
    ArenaLabSession,
    ArenaScoreEvent,
)
from CTFd.plugins.arena.services import settings as event_settings


def _now():
    return datetime.datetime.utcnow()


# ---------------------------------------------------------------- providers


def save_provider(data, row=None):
    creating = row is None
    row = row or ArenaLabProvider()
    if creating or "name" in data:
        row.name = v.string(
            data, "name", required=True, max_len=64, min_len=2, allow_empty=False
        )
    if creating:
        row.adapter = v.choice(data, "adapter", LAB_ADAPTERS, required=True)
    if "base_url" in data or (creating and row.adapter == "generic_http"):
        url = v.string(
            data, "base_url", required=row.adapter == "generic_http", max_len=255
        )
        if url:
            try:
                check_url(url)
            except SSRFError as e:
                raise v.ValidationError("base_url", str(e))
        row.base_url = url
    if "token" in data:  # write-only credential
        token = v.string(data, "token", max_len=4096)
        row.credentials_enc = encrypt({"token": token}) if token else None
    if "config" in data:
        cfg = data["config"] or {}
        if not isinstance(cfg, dict) or set(cfg) - {
            "connect_timeout",
            "read_timeout",
            "retries",
            "simulate_failure",
        }:
            raise v.ValidationError(
                "config", "allowed keys: connect_timeout, read_timeout, retries"
            )
        for k in ("connect_timeout", "read_timeout"):
            if k in cfg and not (
                isinstance(cfg[k], (int, float)) and 0.5 <= cfg[k] <= 60
            ):
                raise v.ValidationError(f"config.{k}", "must be 0.5-60 seconds")
        if "retries" in cfg and not (
            isinstance(cfg["retries"], int) and 0 <= cfg["retries"] <= 5
        ):
            raise v.ValidationError("config.retries", "must be 0-5")
        row.config = cfg
    if "enabled" in data:
        row.enabled = bool(v.boolean(data, "enabled"))
    try:
        if creating:
            db.session.add(row)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError(409, "A provider with this name exists")
    return row


def check_health(row):
    try:
        result = make_provider(row).health()
        ok_ = result.get("status") == "ok"
    except ProviderError as e:
        result, ok_ = {"status": "error", "error": str(e)[:200]}, False
    row.last_health = dict(result, ok=ok_)
    row.last_health_at = _now()
    db.session.commit()
    return row.last_health


def serialize_provider(row):
    return {
        "id": row.id,
        "name": row.name,
        "adapter": row.adapter,
        "base_url": row.base_url,
        "has_credentials": bool(row.credentials_enc),
        "config": row.config or {},
        "enabled": row.enabled,
        "last_health": row.last_health,
        "last_health_at": v.iso(row.last_health_at),
    }


# --------------------------------------------------------------- event labs


def create_event_lab(event, data):
    pid = v.integer(data, "provider_id", required=True, minimum=1)
    provider = db.session.get(ArenaLabProvider, pid)
    if provider is None or not provider.enabled:
        raise v.ValidationError("provider_id", "unknown or disabled provider")
    template = v.string(
        data, "template_id", required=True, max_len=128, allow_empty=False
    )
    lab = ArenaEventLab(
        event_id=event.id,
        provider_id=pid,
        template_id=template,
        name=v.string(data, "name", required=True, max_len=128, allow_empty=False),
        description=v.string(data, "description", max_len=5000),
        ttl_minutes=v.integer(data, "ttl_minutes", minimum=5, maximum=1440) or 120,
        max_sessions=v.integer(data, "max_sessions", minimum=1, maximum=10000) or 50,
        completion_points=v.integer(
            data, "completion_points", minimum=0, maximum=100000
        )
        or 0,
    )
    try:
        db.session.add(lab)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError(409, "This lab template is already attached to the event")
    return lab


def serialize_event_lab(lab):
    return {
        "id": lab.id,
        "provider_id": lab.provider_id,
        "template_id": lab.template_id,
        "name": lab.name,
        "description": lab.description,
        "ttl_minutes": lab.ttl_minutes,
        "max_sessions": lab.max_sessions,
        "completion_points": lab.completion_points,
        "enabled": lab.enabled,
    }


# ----------------------------------------------------------------- sessions


def active_key(lab, entity_key):
    return f"lab:{lab.event_id}:{lab.id}:{entity_key}"


def current(lab, entity_key):
    return ArenaLabSession.query.filter_by(
        active_key=active_key(lab, entity_key)
    ).first()


def request_session(event, lab, entity, user):
    if not lab.enabled:
        raise ApiError(404)
    existing = current(lab, entity.key)
    if existing is not None:
        return existing, False
    s = event_settings.effective(event)
    mine = ArenaLabSession.query.filter(
        ArenaLabSession.event_id == event.id,
        ArenaLabSession.entity_key == entity.key,
        ArenaLabSession.status.in_(ACTIVE_LAB_STATUSES),
    ).count()
    if mine >= s["max_instances_per_entity"]:
        raise ApiError(429, "Lab quota reached; release a lab first", code="quota")
    total = ArenaLabSession.query.filter(
        ArenaLabSession.event_lab_id == lab.id,
        ArenaLabSession.status.in_(ACTIVE_LAB_STATUSES),
    ).count()
    if total >= lab.max_sessions:
        raise ApiError(429, "This lab is at capacity; try later", code="capacity")
    sess = ArenaLabSession(
        uuid=str(uuid.uuid4()),
        event_id=event.id,
        event_lab_id=lab.id,
        provider_id=lab.provider_id,
        entity_key=entity.key,
        user_id=user.id,
        status="requested",
        active_key=active_key(lab, entity.key),
        expires_at=_now() + datetime.timedelta(minutes=lab.ttl_minutes),
    )
    try:
        db.session.add(sess)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return current(lab, entity.key), False
    _enqueue("labs.provision", sess)
    return sess, True


def _enqueue(kind, sess, **extra):
    from CTFd.plugins.arena.services import jobs

    try:
        jobs.enqueue(
            kind,
            {"session_id": sess.id, **extra},
            dedupe_key=f"{kind}:{sess.uuid}",
            event_id=sess.event_id,
            priority=25,
        )
    except jobs.QueueFull:
        if kind == "labs.provision":
            sess.status, sess.active_key, sess.error = "failed", None, "queue full"
            db.session.commit()
        raise ApiError(503, "Lab queue is full; try again later", code="queue_full")


def _provider(sess):
    row = db.session.get(ArenaLabProvider, sess.provider_id)
    if row is None or not row.enabled:
        raise ProviderError("provider disabled")
    return make_provider(row)


def provision(sess):
    if sess.status not in ("requested", "provisioning"):
        return {"skipped": sess.status}
    lab = db.session.get(ArenaEventLab, sess.event_lab_id)
    sess.status = "provisioning"
    db.session.commit()
    provider = _provider(sess)
    ttl = max(60, int((sess.expires_at - _now()).total_seconds()))
    created = provider.create(lab.template_id, sess.uuid, ttl, owner=sess.entity_key)
    sess.external_id = created["id"]
    db.session.commit()
    conn = (
        provider.connection(sess.external_id) if created["status"] == "ready" else None
    )
    db.session.refresh(sess)
    if sess.status != "provisioning":  # released meanwhile
        provider.release(sess.external_id)
        return {"status": "released-during-provision"}
    sess.status = "ready" if created["status"] == "ready" else "provisioning"
    sess.connection_enc = encrypt(conn) if conn else None
    db.session.commit()
    return {"status": sess.status}


def release(sess, reason=None):
    if sess.status not in ACTIVE_LAB_STATUSES:
        return sess
    t = ArenaLabSession.__table__
    db.session.execute(
        update(t)
        .where(t.c.id == sess.id)
        .values(status="stopping", active_key=None, error=reason, connection_enc=None)
    )
    db.session.commit()
    db.session.refresh(sess)
    _enqueue("labs.release", sess)
    return sess


def destroy(sess, final="released"):
    if sess.external_id:
        _provider(sess).release(sess.external_id)
    sess.status = final if sess.status != "expired" else "expired"
    sess.active_key, sess.connection_enc = None, None
    db.session.commit()
    return {"status": sess.status}


def reset(sess):
    if sess.status != "ready" or not sess.external_id:
        raise ApiError(409, "Lab is not ready")
    _enqueue("labs.reset", sess)


def sync(limit=200):
    """Poll provider status; record verified completions (server-side only)."""
    from CTFd.plugins.arena.services import activity, scoreboard

    rows = (
        ArenaLabSession.query.filter(
            ArenaLabSession.status.in_(("provisioning", "ready")),
            ArenaLabSession.external_id.isnot(None),
        )
        .limit(limit)
        .all()
    )
    changed = 0
    for sess in rows:
        try:
            provider = _provider(sess)
            st = provider.status(sess.external_id)
            if sess.status == "provisioning" and st["status"] == "ready":
                sess.connection_enc = encrypt(provider.connection(sess.external_id))
                sess.status = "ready"
                changed += 1
            elif st["status"] in ("expired", "failed", "stopped"):
                sess.status = "failed" if st["status"] == "failed" else "expired"
                sess.active_key, sess.connection_enc = None, None
                changed += 1
            if st["completed"] and sess.completed_at is None:
                sess.completed_at = _now()
                lab = db.session.get(ArenaEventLab, sess.event_lab_id)
                if lab.completion_points:
                    db.session.add(
                        ArenaScoreEvent(
                            event_id=sess.event_id,
                            entity_key=sess.entity_key,
                            user_id=sess.user_id,
                            component="bonus",
                            points=lab.completion_points,
                            source="lab_completion",
                            source_ref=f"{lab.id}:{sess.entity_key}",  # once per entity and lab
                            reason=f"Completed lab {lab.name}"[:255],
                        )
                    )
                db.session.flush()
                activity.record(
                    "lab.complete",
                    user_id=sess.user_id,
                    event_id=sess.event_id,
                    target=("lab", lab.id),
                )
                scoreboard.invalidate(sess.event_id)
                changed += 1
            db.session.commit()
        except IntegrityError:
            db.session.rollback()  # completion already scored for this entity+lab
        except ProviderError:
            db.session.rollback()  # provider down: retry next run
    return changed


def expire_due(now=None):
    now = now or _now()
    rows = (
        ArenaLabSession.query.filter(
            ArenaLabSession.status.in_(ACTIVE_LAB_STATUSES),
            ArenaLabSession.expires_at < now,
        )
        .limit(500)
        .all()
    )
    for sess in rows:
        sess.status, sess.active_key, sess.connection_enc = "expired", None, None
        db.session.commit()
        try:
            _enqueue("labs.release", sess, final="expired")
        except ApiError:
            pass  # next run retries
    return len(rows)


def serialize_session(sess, owner=False, admin=False):
    data = {
        "id": sess.id,
        "lab_id": sess.event_lab_id,
        "status": sess.status,
        "expires_at": v.iso(sess.expires_at),
        "completed": sess.completed_at is not None,
        "created_at": v.iso(sess.created_at),
    }
    if owner and sess.status == "ready":
        data["connection"] = decrypt(sess.connection_enc)
    if admin:
        data.update(
            uuid=sess.uuid,
            entity_key=sess.entity_key,
            user_id=sess.user_id,
            external_id=sess.external_id,
            error=sess.error,
        )
    elif sess.status == "failed":
        data["error"] = "The lab could not be started"
    return data

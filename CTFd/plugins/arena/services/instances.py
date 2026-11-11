"""Challenge instance lifecycle (Phase 8).

request → (job instances.provision) → running → (release | expiry) →
(job instances.destroy) → stopped/expired. Runner calls never happen inside
participant HTTP requests; they run in worker jobs. Concurrency limits:
* one active instance per (event, challenge, entity): UNIQUE active_key
* per-entity cap: settings.max_instances_per_entity
* per-event cap: ARENA_MAX_INSTANCES_PER_EVENT (default 200)
"""

import datetime
import uuid

from flask import current_app
from sqlalchemy import and_, func, update
from sqlalchemy.exc import IntegrityError

from CTFd.models import db
from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.api import ApiError
from CTFd.plugins.arena.execution.client import RunnerError, get_client
from CTFd.plugins.arena.models import (
    ACTIVE_INSTANCE_STATUSES,
    ArenaImage,
    ArenaInstance,
)
from CTFd.plugins.arena.services import settings as event_settings
from CTFd.plugins.arena.validation import ValidationError

DIGEST_REF = r"^[a-z0-9][a-z0-9._/:-]{0,200}@sha256:[0-9a-f]{64}$"


def _now():
    return datetime.datetime.utcnow()


# ------------------------------------------------------------------ images


def validate_image_data(data, creating=True):
    import re

    out = {}
    if creating or "name" in data:
        out["name"] = v.string(
            data, "name", required=True, max_len=64, min_len=2, allow_empty=False
        )
    if creating or "version" in data:
        out["version"] = v.string(
            data, "version", required=True, max_len=32, allow_empty=False
        )
    if creating or "reference" in data:
        ref = v.string(data, "reference", required=True, max_len=255)
        if not re.match(DIGEST_REF, ref or ""):
            raise ValidationError(
                "reference", "must be pinned by digest: repo@sha256:<64 hex>"
            )
        out["reference"] = ref
    if creating or "ports" in data:
        ports = data.get("ports", [])
        if (
            not isinstance(ports, list)
            or len(ports) > 8
            or not all(isinstance(p, int) and 1 <= p <= 65535 for p in ports)
        ):
            raise ValidationError("ports", "must be a list of up to 8 TCP ports")
        out["ports"] = ports
    for f, lo, hi, d in (
        ("cpu_millis", 50, 4000, 500),
        ("memory_mb", 16, 8192, 256),
        ("pids", 8, 4096, 128),
        ("ttl_seconds", 300, 86400, 3600),
    ):
        if creating or f in data:
            val = v.integer(data, f, minimum=lo, maximum=hi)
            out[f] = val if val is not None else d
    if "enabled" in data:
        out["enabled"] = bool(v.boolean(data, "enabled"))
    return out


def create_image(data):
    img = ArenaImage(**validate_image_data(data))
    try:
        db.session.add(img)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError(409, "Image name/version or reference already registered")
    return img


def validate_with_runner(img):
    try:
        res = get_client().check_image(img.reference)
    except RunnerError as e:
        raise ApiError(503, f"Runner unavailable: {e}")
    if not res.get("allowed"):
        img.validated_at = None
        db.session.commit()
        raise ApiError(
            409,
            "Runner does not allow this image (add it to ARENA_RUNNER_ALLOWED_IMAGES)",
        )
    img.validated_at = _now()
    db.session.commit()
    return img


def serialize_image(img):
    return {
        "id": img.id,
        "name": img.name,
        "version": img.version,
        "reference": img.reference,
        "ports": img.ports,
        "cpu_millis": img.cpu_millis,
        "memory_mb": img.memory_mb,
        "pids": img.pids,
        "ttl_seconds": img.ttl_seconds,
        "enabled": img.enabled,
        "validated_at": v.iso(img.validated_at),
    }


# --------------------------------------------------------------- instances


def active_key(event_id, ec_id, entity_key, purpose="challenge"):
    return f"{purpose}:{event_id}:{ec_id}:{entity_key}"


def current(event, ec, entity_key):
    return ArenaInstance.query.filter_by(
        event_id=event.id, active_key=active_key(event.id, ec.id, entity_key)
    ).first()


def request_instance(event, ec, entity, user):
    if ec.image_id is None:
        raise ApiError(404, "This challenge has no instance", code="no_instance")
    img = db.session.get(ArenaImage, ec.image_id)
    if img is None or not img.enabled:
        raise ApiError(409, "Challenge image is unavailable", code="image_unavailable")
    existing = current(event, ec, entity.key)
    if existing is not None:
        return existing, False
    s = event_settings.effective(event)
    # "Holding a slot" == active_key set (the same marker the UNIQUE index
    # uses). Status alone is ambiguous: a released instance ends in
    # "stopped" with active_key cleared and must not count against quotas.
    per_entity = ArenaInstance.query.filter(
        ArenaInstance.event_id == event.id,
        ArenaInstance.entity_key == entity.key,
        ArenaInstance.active_key.isnot(None),
    ).count()
    if per_entity >= s["max_instances_per_entity"]:
        raise ApiError(
            429, "Instance quota reached; release another instance first", code="quota"
        )
    per_event = ArenaInstance.query.filter(
        ArenaInstance.event_id == event.id,
        ArenaInstance.active_key.isnot(None),
    ).count()
    if per_event >= int(current_app.config.get("ARENA_MAX_INSTANCES_PER_EVENT", 200)):
        raise ApiError(
            429, "Event instance capacity reached; try later", code="capacity"
        )
    ttl = min(img.ttl_seconds, s["instance_ttl_minutes"] * 60)
    inst = ArenaInstance(
        uuid=str(uuid.uuid4()),
        event_id=event.id,
        event_challenge_id=ec.id,
        image_id=img.id,
        entity_key=entity.key,
        user_id=user.id,
        status="requested",
        active_key=active_key(event.id, ec.id, entity.key),
        expires_at=_now() + datetime.timedelta(seconds=ttl),
    )
    try:
        db.session.add(inst)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # concurrent request won: return that instance
        return current(event, ec, entity.key), False
    _enqueue("instances.provision", inst)
    return inst, True


def _enqueue(kind, inst):
    from CTFd.plugins.arena.services import jobs

    try:
        jobs.enqueue(
            kind,
            {"instance_id": inst.id},
            dedupe_key=f"{kind}:{inst.uuid}",
            event_id=inst.event_id,
            priority=20,
        )
    except jobs.QueueFull:
        inst.status, inst.error, inst.active_key = "failed", "queue full", None
        db.session.commit()
        raise ApiError(
            503, "Instance queue is full; try again later", code="queue_full"
        )


def release(inst, reason="released"):
    if inst.status not in ACTIVE_INSTANCE_STATUSES:
        return inst
    t = ArenaInstance.__table__
    db.session.execute(
        update(t)
        .where(t.c.id == inst.id)
        .values(
            status="stopping",
            active_key=None,
            error=None if reason == "released" else reason,
        )
    )
    db.session.commit()
    db.session.refresh(inst)
    _enqueue("instances.destroy", inst)
    return inst


def restart(inst):
    if inst.status not in ("running", "stopped"):
        raise ApiError(409, "Instance cannot be restarted in its current state")
    _enqueue("instances.restart", inst)
    return inst


def _spec(inst):
    from CTFd.plugins.arena.models import ArenaEvent, ArenaEventChallenge
    from CTFd.plugins.arena.services.submissions import per_entity_flag

    img = db.session.get(ArenaImage, inst.image_id)
    env = {}
    ec = (
        db.session.get(ArenaEventChallenge, inst.event_challenge_id)
        if inst.event_challenge_id
        else None
    )
    if ec is not None and ec.per_entity_flag:
        env["ARENA_FLAG"] = per_entity_flag(
            db.session.get(ArenaEvent, inst.event_id), ec, inst.entity_key
        )
    ttl = (
        max(60, int((inst.expires_at - _now()).total_seconds()))
        if inst.expires_at
        else img.ttl_seconds
    )
    return {
        "instance_id": inst.uuid,
        "image": img.reference,
        "ports": img.ports,
        "cpu_millis": img.cpu_millis,
        "memory_mb": img.memory_mb,
        "pids": img.pids,
        "ttl_seconds": ttl,
        "env": env,
        "labels": {
            "event": str(inst.event_id),
            "entity": inst.entity_key.replace(":", "_"),
            "purpose": inst.purpose,
        },
    }


def provision(inst):
    """Worker job body. Idempotent: the runner create is keyed by uuid."""
    if inst.status not in ("requested", "starting"):
        return {"skipped": inst.status}
    inst.status = "starting"
    db.session.commit()
    result = get_client().create(_spec(inst))  # RunnerError -> job retry
    if result.get("status") == "failed":
        inst.status, inst.error, inst.active_key = (
            "failed",
            (result.get("error") or "failed")[:255],
            None,
        )
        db.session.commit()
        return {"status": "failed"}
    db.session.refresh(inst)
    if inst.status != "starting":  # released while provisioning
        get_client().delete(inst.uuid)
        return {"status": "released-during-provision"}
    inst.status = "running"
    inst.host = result.get("host")
    inst.ports = result.get("ports") or {}
    inst.last_status_at = _now()
    db.session.commit()
    return {"status": "running"}


def destroy(inst, final_status="stopped"):
    get_client().delete(inst.uuid)  # idempotent on the runner
    t = ArenaInstance.__table__
    db.session.execute(
        update(t)
        .where(and_(t.c.id == inst.id))
        .values(
            status=final_status if inst.status != "expired" else "expired",
            active_key=None,
            host=None,
            ports=None,
            last_status_at=_now(),
        )
    )
    db.session.commit()
    return {"status": final_status}


def expire_due(now=None):
    """Periodic: release instances past their expiry."""
    now = now or _now()
    rows = (
        ArenaInstance.query.filter(
            ArenaInstance.status.in_(("requested", "starting", "running", "stopped")),
            ArenaInstance.active_key.isnot(None),  # released rows are already done
            ArenaInstance.expires_at < now,
        )
        .limit(500)
        .all()
    )
    for inst in rows:
        t = ArenaInstance.__table__
        db.session.execute(
            update(t).where(t.c.id == inst.id).values(status="expired", active_key=None)
        )
        db.session.commit()
        _enqueue_destroy_quiet(inst)
    return len(rows)


def _enqueue_destroy_quiet(inst):
    from CTFd.plugins.arena.services import jobs

    try:
        jobs.enqueue(
            "instances.destroy",
            {"instance_id": inst.id, "final": "expired"},
            dedupe_key=f"instances.destroy:{inst.uuid}",
            event_id=inst.event_id,
            priority=30,
        )
    except jobs.QueueFull:
        pass  # next expiry run retries; runner reaper also enforces TTL


def sync_status(limit=200):
    """Periodic: reconcile running instances with the runner."""
    rows = (
        ArenaInstance.query.filter(ArenaInstance.status == "running")
        .order_by(ArenaInstance.last_status_at)
        .limit(limit)
        .all()
    )
    client = get_client()
    changed = 0
    for inst in rows:
        try:
            st = client.get(inst.uuid).get("status")
        except RunnerError:
            break  # runner down: try again next time
        if st in ("failed", "removed", "expired"):
            inst.status, inst.active_key, inst.error = (
                "failed",
                None,
                f"runner reports {st}",
            )
            changed += 1
        inst.last_status_at = _now()
    db.session.commit()
    return changed


def serialize(inst, include_internal=False):
    data = {
        "id": inst.id,
        "status": inst.status,
        "challenge_id": inst.event_challenge_id,
        "host": inst.host if inst.status == "running" else None,
        "ports": inst.ports if inst.status == "running" else None,
        "expires_at": v.iso(inst.expires_at),
        "created_at": v.iso(inst.created_at),
    }
    if include_internal:
        data.update(
            uuid=inst.uuid,
            entity_key=inst.entity_key,
            user_id=inst.user_id,
            error=inst.error,
            image_id=inst.image_id,
            purpose=inst.purpose,
        )
    elif inst.status == "failed":
        data["error"] = "The instance could not be started"
    return data


def counts_by_status(event):
    return dict(
        db.session.query(ArenaInstance.status, func.count(ArenaInstance.id))
        .filter(ArenaInstance.event_id == event.id)
        .group_by(ArenaInstance.status)
    )

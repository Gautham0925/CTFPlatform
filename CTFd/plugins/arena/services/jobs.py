"""DB-backed job queue (Phase 6).

Guarantees
* At most one worker runs a job: claim is ``UPDATE … WHERE id=? AND
  status='queued'`` (rowcount 1 wins).
* A worker whose lease expired cannot record a result: completion is
  ``UPDATE … WHERE id=? AND locked_by=<me> AND status='running'``.
* Active duplicates are impossible: ``dedupe_key`` is UNIQUE while the job is
  queued/running and cleared when it finishes.
* Bounded: at most ARENA_JOBS_MAX_QUEUED (per kind) queued jobs.
* Failures retry with exponential backoff + jitter, then go to 'dead'
  (dead-letter) and can be retried manually.
Handlers must be idempotent (they can run again after a crash).
"""

import datetime
import logging
import random

from flask import current_app
from sqlalchemy import and_, func, update
from sqlalchemy.exc import IntegrityError

from CTFd.models import db
from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.models import ArenaJob, ArenaLease

log = logging.getLogger("arena.jobs")

HANDLERS = {}
DEFAULT_LEASE_SECONDS = 120
BACKOFF_BASE = 5
BACKOFF_CAP = 600


class QueueFull(Exception):
    pass


class UnknownJob(Exception):
    pass


def handler(kind, max_attempts=5, lease_seconds=DEFAULT_LEASE_SECONDS):
    """Register an idempotent job handler: fn(payload: dict, job) -> dict|None."""

    def deco(fn):
        HANDLERS[kind] = {
            "fn": fn,
            "max_attempts": max_attempts,
            "lease": lease_seconds,
        }
        return fn

    return deco


def _now():
    return datetime.datetime.utcnow()


def _max_queued():
    return int(current_app.config.get("ARENA_JOBS_MAX_QUEUED", 1000))


# ------------------------------------------------------------------ enqueue


def enqueue(
    kind,
    payload=None,
    *,
    dedupe_key=None,
    run_after=None,
    event_id=None,
    priority=100,
    max_attempts=None,
):
    """Queue a job. With dedupe_key, an active job with the same key is
    returned instead of creating a duplicate."""
    if kind not in HANDLERS:
        raise UnknownJob(kind)
    if dedupe_key:
        existing = ArenaJob.query.filter_by(dedupe_key=dedupe_key).first()
        if existing is not None:
            return existing
    queued = (
        db.session.query(func.count(ArenaJob.id))
        .filter(ArenaJob.kind == kind, ArenaJob.status == "queued")
        .scalar()
    )
    if queued >= _max_queued():
        raise QueueFull(f"queue for {kind} is full ({queued})")
    job = ArenaJob(
        kind=kind,
        payload=payload or {},
        status="queued",
        priority=priority,
        attempts=0,
        max_attempts=max_attempts or HANDLERS[kind]["max_attempts"],
        run_after=run_after or _now(),
        dedupe_key=dedupe_key,
        event_id=event_id,
    )
    try:
        db.session.add(job)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = ArenaJob.query.filter_by(dedupe_key=dedupe_key).first()
        if existing is not None:
            return existing
        raise
    return job


# -------------------------------------------------------------------- claim


def claim(worker_id, kinds=None, now=None):
    """Claim one runnable job (or None)."""
    now = now or _now()
    q = ArenaJob.query.with_entities(ArenaJob.id, ArenaJob.kind).filter(
        ArenaJob.status == "queued", ArenaJob.run_after <= now
    )
    if kinds:
        q = q.filter(ArenaJob.kind.in_(kinds))
    candidates = q.order_by(ArenaJob.priority, ArenaJob.id).limit(10).all()
    db.session.rollback()  # end the read transaction (fresh snapshot below)
    t = ArenaJob.__table__
    for job_id, kind in candidates:
        lease = HANDLERS.get(kind, {}).get("lease", DEFAULT_LEASE_SECONDS)
        res = db.session.execute(
            update(t)
            .where(and_(t.c.id == job_id, t.c.status == "queued"))
            .values(
                status="running",
                locked_by=worker_id,
                lease_until=now + datetime.timedelta(seconds=lease),
                attempts=t.c.attempts + 1,
                started_at=now,
            )
        )
        db.session.commit()
        if res.rowcount == 1:
            return db.session.get(ArenaJob, job_id)
    return None


def renew(job, worker_id, now=None):
    now = now or _now()
    lease = HANDLERS.get(job.kind, {}).get("lease", DEFAULT_LEASE_SECONDS)
    t = ArenaJob.__table__
    res = db.session.execute(
        update(t)
        .where(
            and_(t.c.id == job.id, t.c.locked_by == worker_id, t.c.status == "running")
        )
        .values(lease_until=now + datetime.timedelta(seconds=lease))
    )
    db.session.commit()
    return res.rowcount == 1


def _finish(job, worker_id, values):
    t = ArenaJob.__table__
    res = db.session.execute(
        update(t)
        .where(
            and_(t.c.id == job.id, t.c.locked_by == worker_id, t.c.status == "running")
        )
        .values(**values)
    )
    db.session.commit()
    return res.rowcount == 1


def complete(job, worker_id, result=None):
    """Mark done. False if this worker lost the lease meanwhile."""
    return _finish(
        job,
        worker_id,
        {
            "status": "done",
            "result": result,
            "finished_at": _now(),
            "locked_by": None,
            "lease_until": None,
            "dedupe_key": None,
            "last_error": None,
        },
    )


def backoff_seconds(attempts):
    delay = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** max(0, attempts - 1)))
    return delay * (0.75 + random.random() * 0.5)  # noqa: S311 - jitter, not crypto


def fail(job, worker_id, error):
    error = str(error)[:2000]
    if job.attempts >= job.max_attempts:
        return _finish(
            job,
            worker_id,
            {
                "status": "dead",
                "last_error": error,
                "finished_at": _now(),
                "locked_by": None,
                "lease_until": None,
                "dedupe_key": None,
            },
        )
    return _finish(
        job,
        worker_id,
        {
            "status": "queued",
            "last_error": error,
            "locked_by": None,
            "lease_until": None,
            "run_after": _now()
            + datetime.timedelta(seconds=backoff_seconds(job.attempts)),
        },
    )


def recover_stale(now=None):
    """Re-queue (or dead-letter) running jobs whose lease expired (worker
    crashed or was killed). Returns the number of recovered jobs."""
    now = now or _now()
    t = ArenaJob.__table__
    stale = ArenaJob.query.filter(
        ArenaJob.status == "running", ArenaJob.lease_until < now
    ).all()
    n = 0
    for job in stale:
        to_dead = job.attempts >= job.max_attempts
        values = {
            "status": "dead" if to_dead else "queued",
            "locked_by": None,
            "lease_until": None,
            "last_error": "lease expired (worker lost)",
        }
        if to_dead:
            values.update(finished_at=now, dedupe_key=None)
        res = db.session.execute(
            update(t)
            .where(
                and_(t.c.id == job.id, t.c.status == "running", t.c.lease_until < now)
            )
            .values(**values)
        )
        n += res.rowcount
    db.session.commit()
    return n


def retry_dead(job_id):
    t = ArenaJob.__table__
    res = db.session.execute(
        update(t)
        .where(and_(t.c.id == job_id, t.c.status == "dead"))
        .values(
            status="queued",
            attempts=0,
            run_after=_now(),
            finished_at=None,
            last_error=None,
        )
    )
    db.session.commit()
    return res.rowcount == 1


def run_job(job, worker_id):
    """Execute a claimed job; returns final status."""
    spec = HANDLERS.get(job.kind)
    if spec is None:
        _finish(
            job,
            worker_id,
            {
                "status": "dead",
                "last_error": f"no handler for {job.kind}",
                "finished_at": _now(),
                "locked_by": None,
                "dedupe_key": None,
            },
        )
        return "dead"
    try:
        result = spec["fn"](dict(job.payload or {}), job)
        db.session.commit()
    except Exception as e:  # noqa: BLE001 - any handler error is a job failure
        db.session.rollback()
        log.warning("job %s (%s) failed: %s", job.id, job.kind, type(e).__name__)
        fail(job, worker_id, f"{type(e).__name__}: {e}")
        db.session.refresh(job)
        return job.status
    if not complete(job, worker_id, result if isinstance(result, dict) else None):
        log.warning(
            "job %s finished after its lease was lost; result discarded", job.id
        )
        return "lost"
    return "done"


def purge_finished(older_than_days=7, batch=1000):
    cutoff = _now() - datetime.timedelta(days=older_than_days)
    ids = [
        i
        for (i,) in db.session.query(ArenaJob.id)
        .filter(ArenaJob.status.in_(("done", "dead")), ArenaJob.finished_at < cutoff)
        .limit(batch)
    ]
    if ids:
        ArenaJob.query.filter(ArenaJob.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
    return len(ids)


def stats():
    rows = (
        db.session.query(ArenaJob.kind, ArenaJob.status, func.count(ArenaJob.id))
        .group_by(ArenaJob.kind, ArenaJob.status)
        .all()
    )
    out = {}
    for kind, status, n in rows:
        out.setdefault(kind, {})[status] = n
    oldest = (
        db.session.query(func.min(ArenaJob.run_after))
        .filter(ArenaJob.status == "queued")
        .scalar()
    )
    return {"by_kind": out, "oldest_queued": v.iso(oldest)}


# -------------------------------------------------------------------- lease


def acquire_lease(name, holder, seconds, now=None):
    """Acquire or renew a named lease. True if `holder` owns it afterwards."""
    now = now or _now()
    t = ArenaLease.__table__
    until = now + datetime.timedelta(seconds=seconds)
    res = db.session.execute(
        update(t)
        .where(and_(t.c.name == name, (t.c.holder == holder) | (t.c.expires_at < now)))
        .values(holder=holder, expires_at=until)
    )
    db.session.commit()
    if res.rowcount == 1:
        return True
    if db.session.get(ArenaLease, name) is None:
        try:
            db.session.add(ArenaLease(name=name, holder=holder, expires_at=until))
            db.session.commit()
            return True
        except IntegrityError:
            db.session.rollback()
    return False


def advance_schedule(name, bucket):
    """Record that periodic schedule `name` was enqueued for `bucket`.
    True only for the first caller per bucket (stored in arena_leases)."""
    key, value = f"periodic:{name}"[:64], str(int(bucket))
    t = ArenaLease.__table__
    res = db.session.execute(
        update(t)
        .where(and_(t.c.name == key, (t.c.holder.is_(None)) | (t.c.holder != value)))
        .values(holder=value, expires_at=_now())
    )
    db.session.commit()
    if res.rowcount == 1:
        return True
    if db.session.get(ArenaLease, key) is None:
        try:
            db.session.add(ArenaLease(name=key, holder=value, expires_at=_now()))
            db.session.commit()
            return True
        except IntegrityError:
            db.session.rollback()
    return False


def release_lease(name, holder):
    t = ArenaLease.__table__
    db.session.execute(
        update(t)
        .where(and_(t.c.name == name, t.c.holder == holder))
        .values(holder=None, expires_at=_now())
    )
    db.session.commit()


def serialize(job):
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "priority": job.priority,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "run_after": v.iso(job.run_after),
        "event_id": job.event_id,
        "locked_by": job.locked_by,
        "last_error": job.last_error,
        "created_at": v.iso(job.created_at),
        "started_at": v.iso(job.started_at),
        "finished_at": v.iso(job.finished_at),
    }

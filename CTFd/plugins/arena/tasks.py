"""Registered background job handlers and periodic schedules.

Every handler is idempotent: running it twice has the same effect as once.
Imported by load() so web processes can enqueue and workers can execute.
"""

from CTFd.plugins.arena.services import jobs
from CTFd.plugins.arena.worker import periodic


@jobs.handler("events.sync_statuses")
def sync_statuses(payload, job):
    from CTFd.plugins.arena.services import events, scoreboard

    changed = events.sync_statuses()
    for event_id, _old, _new in changed:
        scoreboard.invalidate(event_id)
    return {"changed": [list(c) for c in changed]}


@jobs.handler("scoreboard.snapshot")
def scoreboard_snapshot(payload, job):
    """Persist last-known-good scoreboards for running events."""
    from CTFd.plugins.arena.models import ArenaEvent
    from CTFd.plugins.arena.services import events, scoreboard

    done = []
    q = ArenaEvent.query.filter(ArenaEvent.status.in_(("active", "paused", "upcoming")))
    if payload.get("event_id"):
        q = q.filter(ArenaEvent.id == int(payload["event_id"]))
    for event in q:
        if events.effective_phase(event) in ("active", "paused"):
            scoreboard.save_snapshot(event, "public")
            done.append(event.id)
    return {"events": done}


@jobs.handler("jobs.purge")
def purge(payload, job):
    return {"purged": jobs.purge_finished(older_than_days=int(payload.get("days", 7)))}


periodic("sync-statuses", 30, "events.sync_statuses")
periodic("scoreboard-snapshots", 60, "scoreboard.snapshot")
periodic("jobs-purge", 3600, "jobs.purge")


@jobs.handler("analytics.rollup", max_attempts=3, lease_seconds=300)
def analytics_rollup(payload, job):
    """Idempotent: recomputes recent hourly buckets + challenge stats."""
    import datetime

    from CTFd.plugins.arena.models import ArenaEvent
    from CTFd.plugins.arena.services import analytics, events

    now = datetime.datetime.utcnow()
    q = ArenaEvent.query
    if payload.get("event_id"):
        q = q.filter(ArenaEvent.id == int(payload["event_id"]))
    else:
        q = q.filter(
            ArenaEvent.status.in_(("upcoming", "active", "paused", "completed"))
        )
    done = []
    for event in q:
        phase = events.effective_phase(event, now)
        recent_end = event.ends_at and now - event.ends_at < datetime.timedelta(hours=2)
        if payload.get("event_id") or phase in ("active", "paused") or recent_end:
            start = event.starts_at if payload.get("full") else None
            analytics.rollup_hourly(event, start=start, end=now)
            analytics.rollup_challenges(event)
            done.append(event.id)
    return {"events": done}


@jobs.handler("activity.flush", max_attempts=10, lease_seconds=60)
def activity_flush(payload, job):
    from CTFd.plugins.arena.services import activity

    return {"flushed": activity.flush()}


@jobs.handler("activity.prune", max_attempts=3, lease_seconds=600)
def activity_prune(payload, job):
    from CTFd.plugins.arena.services import activity

    return {"pruned": activity.prune()}


periodic("analytics-rollup", 60, "analytics.rollup")
periodic("activity-flush", 5, "activity.flush")
periodic("activity-prune", 86400, "activity.prune")


def _instance(payload):
    from CTFd.models import db
    from CTFd.plugins.arena.models import ArenaInstance

    return db.session.get(ArenaInstance, int(payload["instance_id"]))


@jobs.handler("instances.provision", max_attempts=4, lease_seconds=120)
def instances_provision(payload, job):
    from CTFd.plugins.arena.services import instances

    inst = _instance(payload)
    if inst is None:
        return {"skipped": "missing"}
    try:
        return instances.provision(inst)
    except Exception:
        if job.attempts >= job.max_attempts:  # final attempt: free the slot
            inst.status, inst.active_key, inst.error = (
                "failed",
                None,
                "runner unavailable",
            )
            from CTFd.models import db

            db.session.commit()
        raise


@jobs.handler("instances.destroy", max_attempts=8, lease_seconds=120)
def instances_destroy(payload, job):
    from CTFd.plugins.arena.services import instances

    inst = _instance(payload)
    if inst is None:
        return {"skipped": "missing"}
    return instances.destroy(inst, final_status=payload.get("final", "stopped"))


@jobs.handler("instances.restart", max_attempts=3, lease_seconds=120)
def instances_restart(payload, job):
    from CTFd.models import db
    from CTFd.plugins.arena.execution.client import get_client

    inst = _instance(payload)
    if inst is None or inst.status not in ("running", "stopped"):
        return {"skipped": True}
    client = get_client()
    client.stop(inst.uuid)
    client.start(inst.uuid)
    inst.status = "running"
    db.session.commit()
    return {"status": "running"}


@jobs.handler("instances.expire", max_attempts=3, lease_seconds=120)
def instances_expire(payload, job):
    from CTFd.plugins.arena.services import instances

    return {"expired": instances.expire_due()}


@jobs.handler("instances.sync", max_attempts=3, lease_seconds=120)
def instances_sync(payload, job):
    from CTFd.plugins.arena.services import instances

    return {"changed": instances.sync_status()}


periodic("instances-expire", 30, "instances.expire")
periodic("instances-sync", 60, "instances.sync")


@jobs.handler("ad.tick", max_attempts=3, lease_seconds=60)
def ad_tick(payload, job):
    from CTFd.plugins.arena.models import ArenaEvent
    from CTFd.plugins.arena.services import ad

    out = {}
    q = ArenaEvent.query.filter(
        ArenaEvent.ad_enabled.is_(True),
        ArenaEvent.status.in_(("upcoming", "active", "paused", "completed")),
    )
    for event in q:
        out[event.id] = ad.tick(event)
    return out


@jobs.handler("ad.check", max_attempts=3, lease_seconds=300)
def ad_check(payload, job):
    from CTFd.models import db
    from CTFd.plugins.arena.models import ArenaADRound
    from CTFd.plugins.arena.services import ad

    rnd = db.session.get(ArenaADRound, int(payload["round_id"]))
    if rnd is None or rnd.status != "active":
        return {"skipped": True}
    return ad.run_checks(rnd)


periodic("ad-tick", 5, "ad.tick")


@jobs.handler("scoring.history_snapshot", max_attempts=3, lease_seconds=120)
def scoring_history_snapshot(payload, job):
    """Hourly history snapshot while running; one 'final' after the end."""
    import datetime

    from CTFd.plugins.arena.models import ArenaEvent, ArenaScoreSnapshot
    from CTFd.plugins.arena.services import events, mixed

    now = datetime.datetime.utcnow()
    done = []
    for event in ArenaEvent.query.filter(
        ArenaEvent.status.in_(("active", "paused", "completed"))
    ):
        phase = events.effective_phase(event, now)
        if phase in ("active", "paused"):
            mixed.snapshot(event, "periodic", ref=now.strftime("%Y%m%d%H"))
            done.append(event.id)
        elif (
            phase == "completed"
            and not ArenaScoreSnapshot.query.filter_by(
                event_id=event.id, kind="final"
            ).first()
        ):
            mixed.snapshot(event, "final")
            done.append(event.id)
    return {"events": done}


periodic("scoring-history", 3600, "scoring.history_snapshot")


def _lab_session(payload):
    from CTFd.models import db
    from CTFd.plugins.arena.models import ArenaLabSession

    return db.session.get(ArenaLabSession, int(payload["session_id"]))


@jobs.handler("labs.provision", max_attempts=4, lease_seconds=120)
def labs_provision(payload, job):
    from CTFd.models import db
    from CTFd.plugins.arena.services import labs

    sess = _lab_session(payload)
    if sess is None:
        return {"skipped": "missing"}
    try:
        return labs.provision(sess)
    except Exception:
        if job.attempts >= job.max_attempts:
            sess.status, sess.active_key, sess.error = (
                "failed",
                None,
                "provider unavailable",
            )
            db.session.commit()
        raise


@jobs.handler("labs.release", max_attempts=8, lease_seconds=120)
def labs_release(payload, job):
    from CTFd.plugins.arena.services import labs

    sess = _lab_session(payload)
    return (
        labs.destroy(sess, final=payload.get("final", "released"))
        if sess
        else {"skipped": True}
    )


@jobs.handler("labs.reset", max_attempts=3, lease_seconds=120)
def labs_reset(payload, job):
    from CTFd.plugins.arena.services import labs

    sess = _lab_session(payload)
    if sess is None or sess.status != "ready":
        return {"skipped": True}
    labs._provider(sess).reset(sess.external_id)
    return {"reset": sess.id}


@jobs.handler("labs.sync", max_attempts=3, lease_seconds=120)
def labs_sync(payload, job):
    from CTFd.plugins.arena.services import labs

    return {"changed": labs.sync()}


@jobs.handler("labs.expire", max_attempts=3, lease_seconds=120)
def labs_expire(payload, job):
    from CTFd.plugins.arena.services import labs

    return {"expired": labs.expire_due()}


@jobs.handler("labs.health", max_attempts=2, lease_seconds=120)
def labs_health(payload, job):
    from CTFd.plugins.arena.models import ArenaLabProvider
    from CTFd.plugins.arena.services import labs

    return {
        p.name: labs.check_health(p).get("ok")
        for p in ArenaLabProvider.query.filter_by(enabled=True)
    }


periodic("labs-sync", 30, "labs.sync")
periodic("labs-expire", 60, "labs.expire")
periodic("labs-health", 300, "labs.health")


@jobs.handler("certificates.issue", max_attempts=3, lease_seconds=900)
def certificates_issue(payload, job):
    """Only enqueued through the certificate API, which exists only when
    certificates are enabled; no periodic schedule."""
    from CTFd.models import db
    from CTFd.plugins.arena.models import ArenaCertTemplate
    from CTFd.plugins.arena.services import certificates

    if not certificates.enabled():
        return {"skipped": "certificates disabled"}
    tpl = db.session.get(ArenaCertTemplate, int(payload["template_id"]))
    return certificates.issue(tpl) if tpl else {"skipped": "missing"}

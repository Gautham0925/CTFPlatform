"""Attack & Defense engine (Phase 9).

Round lifecycle (driven by the worker job ``ad.tick``, leader only):
    open round N → generate flags (HMAC, never stored in clear) →
    job ``ad.check``: runner plants flags + checks every team service →
    store checks → at ends_at: close round N (CAS active→closing) → score round
    → closed → open N+1 (until the event ends).

Scoring is computed only from stored checks and captures and written to the
ledger with unique source refs, so re-running a round close is a no-op:
    ad_sla      +service.sla_points        per 'up' check
    ad_attack   +service.attack_points     per accepted capture (attacker)
    ad_defense  -service.loss_points       per stolen flag (victim)
                +service.defense_points    per service-round that was up
                                           and not compromised
Captures are accepted only for flags of other teams, of this event, not
expired (age < ad_flag_lifetime_rounds), after the warm-up rounds, once per
(flag, attacker).
"""

import base64
import datetime
import hashlib
import hmac
import logging

from flask import current_app
from sqlalchemy import and_, func, update
from sqlalchemy.exc import IntegrityError

from CTFd.models import db
from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.api import ApiError
from CTFd.plugins.arena.models import (
    ArenaADCapture,
    ArenaADCheck,
    ArenaADFlag,
    ArenaADRound,
    ArenaADService,
    ArenaADTeamService,
    ArenaEvent,
    ArenaImage,
    ArenaInstance,
    ArenaScoreEvent,
    ArenaTeam,
)
from CTFd.plugins.arena.services import events as event_service
from CTFd.plugins.arena.services import settings as event_settings
from CTFd.plugins.arena.services.entity import team_key

log = logging.getLogger("arena.ad")
FLAG_PREFIX = "FLAG_"
MAX_FLAGS_PER_REQUEST = 50


def _now():
    return datetime.datetime.utcnow()


# --------------------------------------------------------------- flags


def _secret(event):
    base = (
        current_app.config.get("ARENA_FLAG_SECRET") or current_app.config["SECRET_KEY"]
    )
    if isinstance(base, str):
        base = base.encode()
    return hmac.new(base, f"arena-ad:{event.id}".encode(), hashlib.sha256).digest()


def flag_value(event, round_number, team_id, service_id):
    mac = hmac.new(
        _secret(event),
        f"{round_number}:{team_id}:{service_id}".encode(),
        hashlib.sha256,
    )
    return FLAG_PREFIX + base64.b32encode(mac.digest()).decode().rstrip("=")[:32]


def _hash(flag):
    return hashlib.sha256(flag.encode()).hexdigest()


# ------------------------------------------------------------- services


def create_service(event, data):
    if not event.ad_enabled:
        raise ApiError(409, "Attack & Defense is not enabled for this event")
    image_id = v.integer(data, "image_id", required=True, minimum=1)
    img = db.session.get(ArenaImage, image_id)
    if img is None or not img.enabled:
        raise v.ValidationError("image_id", "unknown or disabled image")
    checker = v.string(data, "checker", max_len=255) or "mock"
    if checker != "mock":
        import re

        if not re.match(r"^[a-z0-9][a-z0-9._/:-]{0,200}@sha256:[0-9a-f]{64}$", checker):
            raise v.ValidationError(
                "checker", "must be 'mock' or a digest-pinned checker image"
            )
    svc = ArenaADService(
        event_id=event.id,
        name=v.string(
            data, "name", required=True, max_len=64, min_len=2, allow_empty=False
        ),
        description=v.string(data, "description", max_len=5000),
        image_id=img.id,
        checker=checker,
        attack_points=v.integer(data, "attack_points", minimum=0, maximum=10000) or 10,
        defense_points=v.integer(data, "defense_points", minimum=0, maximum=10000) or 0,
        loss_points=v.integer(data, "loss_points", minimum=0, maximum=10000) or 0,
        sla_points=v.integer(data, "sla_points", minimum=0, maximum=10000) or 0,
    )
    try:
        db.session.add(svc)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError(409, "A service with this name exists")
    return svc


def provision(event):
    """Create one isolated instance per (team, service) via the runner jobs.
    Idempotent: existing team services are kept."""
    from CTFd.plugins.arena.services import instances as inst_service
    from CTFd.plugins.arena.services import jobs

    created = 0
    teams = ArenaTeam.query.filter_by(event_id=event.id, status="active").all()
    services = ArenaADService.query.filter_by(event_id=event.id, enabled=True).all()
    for svc in services:
        img = db.session.get(ArenaImage, svc.image_id)
        for team in teams:
            if ArenaADTeamService.query.filter_by(
                service_id=svc.id, team_id=team.id
            ).first():
                continue
            import uuid

            inst = ArenaInstance(
                uuid=str(uuid.uuid4()),
                event_id=event.id,
                image_id=img.id,
                entity_key=team_key(team.id),
                purpose="ad_service",
                status="requested",
                active_key=f"ad:{event.id}:{svc.id}:{team.id}",
                expires_at=event.ends_at or (_now() + datetime.timedelta(days=2)),
            )
            db.session.add(inst)
            db.session.flush()
            db.session.add(
                ArenaADTeamService(
                    event_id=event.id,
                    service_id=svc.id,
                    team_id=team.id,
                    instance_id=inst.id,
                    status="provisioning",
                )
            )
            db.session.commit()
            jobs.enqueue(
                "instances.provision",
                {"instance_id": inst.id},
                dedupe_key=f"instances.provision:{inst.uuid}",
                event_id=event.id,
                priority=10,
            )
            created += 1
    del inst_service
    return created


# --------------------------------------------------------------- rounds


def current_round(event):
    return (
        ArenaADRound.query.filter_by(event_id=event.id, status="active")
        .order_by(ArenaADRound.number.desc())
        .first()
    )


def last_round(event):
    return (
        ArenaADRound.query.filter_by(event_id=event.id)
        .order_by(ArenaADRound.number.desc())
        .first()
    )


def open_round(event, now=None):
    now = now or _now()
    prev = last_round(event)
    number = (prev.number + 1) if prev else 1
    seconds = event_settings.get(event, "ad_round_seconds")
    rnd = ArenaADRound(
        event_id=event.id,
        number=number,
        status="active",
        starts_at=now,
        ends_at=now + datetime.timedelta(seconds=seconds),
    )
    try:
        db.session.add(rnd)
        db.session.flush()  # UNIQUE(event, number): concurrent openers -> one wins
    except IntegrityError:
        db.session.rollback()
        return current_round(event)
    team_services = ArenaADTeamService.query.filter_by(event_id=event.id).all()
    for ts in team_services:
        db.session.add(
            ArenaADFlag(
                event_id=event.id,
                round_id=rnd.id,
                round_number=number,
                team_id=ts.team_id,
                service_id=ts.service_id,
                flag_hash=_hash(flag_value(event, number, ts.team_id, ts.service_id)),
            )
        )
    db.session.commit()
    from CTFd.plugins.arena.services import jobs

    jobs.enqueue(
        "ad.check",
        {"round_id": rnd.id},
        dedupe_key=f"ad.check:{rnd.id}",
        event_id=event.id,
        priority=5,
    )
    return rnd


def run_checks(rnd):
    """Ask the runner to plant this round's flags and check every service."""
    from CTFd.plugins.arena.execution.client import get_client

    event = db.session.get(ArenaEvent, rnd.event_id)
    if rnd.checks_done:
        return {"skipped": True}
    items = []
    for ts in ArenaADTeamService.query.filter_by(event_id=event.id).all():
        svc = db.session.get(ArenaADService, ts.service_id)
        inst = db.session.get(ArenaInstance, ts.instance_id) if ts.instance_id else None
        items.append(
            {
                "team_id": ts.team_id,
                "service_id": svc.id,
                "checker": svc.checker,
                "host": inst.host if inst else None,
                "ports": inst.ports if inst else None,
                "flag": flag_value(event, rnd.number, ts.team_id, svc.id),
                "mock_status": ts.mock_status,
            }
        )
    result = get_client().ad_check(
        {"round": rnd.number, "event": event.id, "services": items}
    )
    stored = 0
    for r in result.get("results", []):
        status = (
            r.get("status")
            if r.get("status") in ("up", "mumble", "corrupt", "down", "error")
            else "error"
        )
        try:
            db.session.add(
                ArenaADCheck(
                    event_id=event.id,
                    round_id=rnd.id,
                    team_id=int(r["team_id"]),
                    service_id=int(r["service_id"]),
                    status=status,
                    detail=str(r.get("detail") or "")[:255],
                )
            )
            db.session.flush()
            stored += 1
        except IntegrityError:
            db.session.rollback()  # already stored (job retry): idempotent
        ArenaADTeamService.query.filter_by(
            service_id=int(r["service_id"]), team_id=int(r["team_id"])
        ).update({"status": status, "last_check_at": _now()})
    rnd.checks_done = True
    db.session.commit()
    return {"checks": stored}


def close_round(rnd):
    """CAS active→closing, score, mark closed. Safe to call repeatedly."""
    t = ArenaADRound.__table__
    res = db.session.execute(
        update(t)
        .where(and_(t.c.id == rnd.id, t.c.status == "active"))
        .values(status="closing")
    )
    db.session.commit()
    db.session.refresh(rnd)
    if res.rowcount != 1 and rnd.status != "closing":
        return {"skipped": rnd.status}
    score_round(rnd)
    rnd.status, rnd.closed_at = "closed", _now()
    db.session.commit()
    from CTFd.plugins.arena.services import mixed

    mixed.snapshot(
        db.session.get(ArenaEvent, rnd.event_id), "round", ref=str(rnd.number)
    )
    from CTFd.plugins.arena.services import scoreboard

    scoreboard.invalidate(rnd.event_id)
    return {"closed": rnd.number}


def _ledger(event_id, key, component, points, source, ref, when):
    if not points:
        return
    try:
        db.session.add(
            ArenaScoreEvent(
                event_id=event_id,
                entity_key=key,
                component=component,
                points=points,
                source=source,
                source_ref=ref,
                created_at=when,
            )
        )
        db.session.flush()
    except IntegrityError:
        db.session.rollback()  # already scored


def score_round(rnd):
    when = rnd.ends_at
    services = {s.id: s for s in ArenaADService.query.filter_by(event_id=rnd.event_id)}
    checks = ArenaADCheck.query.filter_by(round_id=rnd.id).all()
    captures = ArenaADCapture.query.filter_by(round_id=rnd.id).all()
    stolen = {(c.victim_team_id, c.service_id) for c in captures}
    for chk in checks:
        svc = services.get(chk.service_id)
        if svc is None:
            continue
        key = team_key(chk.team_id)
        if chk.status == "up":
            _ledger(
                rnd.event_id,
                key,
                "ad_sla",
                svc.sla_points,
                "ad_sla",
                f"{rnd.id}:{chk.team_id}:{chk.service_id}",
                when,
            )
            if (chk.team_id, chk.service_id) not in stolen:
                _ledger(
                    rnd.event_id,
                    key,
                    "ad_defense",
                    svc.defense_points,
                    "ad_defended",
                    f"{rnd.id}:{chk.team_id}:{chk.service_id}",
                    when,
                )
    for cap in captures:
        svc = services.get(cap.service_id)
        if svc is None:
            continue
        _ledger(
            rnd.event_id,
            team_key(cap.attacker_team_id),
            "ad_attack",
            svc.attack_points,
            "ad_capture",
            str(cap.id),
            when,
        )
        _ledger(
            rnd.event_id,
            team_key(cap.victim_team_id),
            "ad_defense",
            -svc.loss_points,
            "ad_flag_lost",
            str(cap.id),
            when,
        )
    db.session.commit()


def tick(event, now=None):
    """One scheduler step for an A&D event."""
    now = now or _now()
    if not event.ad_enabled:
        return None
    phase = event_service.effective_phase(event, now)
    rnd = current_round(event)
    if rnd is not None and rnd.ends_at <= now:
        close_round(rnd)
        rnd = None
    if phase != "active":
        return "idle"
    if (
        rnd is None
        and ArenaADTeamService.query.filter_by(event_id=event.id).first() is not None
    ):
        rnd = open_round(event, now=now)
        return f"opened {rnd.number}"
    return "running"


# ------------------------------------------------------------- captures


def submit_flags(event, team, user, flags):
    if not event_service.is_running(event):
        raise ApiError(403, "The event is not running", code="event_not_running")
    if not isinstance(flags, list) or not flags or len(flags) > MAX_FLAGS_PER_REQUEST:
        raise ApiError(
            400, f"flags must be a list of 1-{MAX_FLAGS_PER_REQUEST} strings"
        )
    rnd = current_round(event)
    if rnd is None:
        raise ApiError(409, "No round is running", code="no_round")
    s = event_settings.effective(event)
    _ratelimit(event, team, len(flags), s["ad_flag_submissions_per_minute"])
    results = []
    for raw in flags:
        flag = raw.strip() if isinstance(raw, str) else ""
        results.append(
            {"flag": flag[:64], "result": _accept(event, team, user, rnd, flag, s)}
        )
    return results


def _ratelimit(event, team, n, limit):
    from CTFd.plugins.arena import arena_cache

    key = f"arena:adrl:{event.id}:{team.id}:{int(_now().timestamp()) // 60}"
    count = arena_cache.incr(key)
    if count is not None and count > limit:
        raise ApiError(429, "Too many flag submissions; slow down", code="ratelimited")


def _accept(event, team, user, rnd, flag, s):
    if not flag.startswith(FLAG_PREFIX) or len(flag) > 64:
        return "invalid"
    row = ArenaADFlag.query.filter_by(flag_hash=_hash(flag)).first()
    if row is None or row.event_id != event.id:  # never reveal flags of other events
        return "invalid"
    if row.team_id == team.id:
        return "own_flag"
    if rnd.number <= s["ad_warmup_rounds"]:
        return "warmup"
    if rnd.number - row.round_number >= s["ad_flag_lifetime_rounds"]:
        return "expired"
    try:
        db.session.add(
            ArenaADCapture(
                event_id=event.id,
                flag_id=row.id,
                attacker_team_id=team.id,
                victim_team_id=row.team_id,
                service_id=row.service_id,
                round_id=rnd.id,
                user_id=user.id,
            )
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return "duplicate"
    return "accepted"


# --------------------------------------------------------------- views


def uptime(event, team_id=None):
    q = db.session.query(
        ArenaADCheck.team_id,
        ArenaADCheck.service_id,
        ArenaADCheck.status,
        func.count(ArenaADCheck.id),
    ).filter(ArenaADCheck.event_id == event.id)
    if team_id is not None:
        q = q.filter(ArenaADCheck.team_id == team_id)
    out = {}
    for tid, sid, status, n in q.group_by(
        ArenaADCheck.team_id, ArenaADCheck.service_id, ArenaADCheck.status
    ):
        row = out.setdefault(
            (tid, sid), {"team_id": tid, "service_id": sid, "checks": 0, "up": 0}
        )
        row["checks"] += n
        if status == "up":
            row["up"] += n
    for row in out.values():
        row["uptime"] = round(row["up"] / row["checks"], 4) if row["checks"] else 0.0
    return list(out.values())


def serialize_round(r):
    return {
        "id": r.id,
        "number": r.number,
        "status": r.status,
        "starts_at": v.iso(r.starts_at),
        "ends_at": v.iso(r.ends_at),
        "closed_at": v.iso(r.closed_at),
        "checks_done": r.checks_done,
    }


def serialize_service(s):
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "image_id": s.image_id,
        "checker": s.checker,
        "attack_points": s.attack_points,
        "defense_points": s.defense_points,
        "loss_points": s.loss_points,
        "sla_points": s.sla_points,
        "enabled": s.enabled,
    }

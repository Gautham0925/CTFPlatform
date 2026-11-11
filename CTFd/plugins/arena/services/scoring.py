"""Centralized scoring service (Phase 5; component weights in Phase 10).

Sources of truth (all event-scoped):
  * arena_solves            — accepted Jeopardy solves; a solve is worth the
                              challenge's *current* value (native semantics for
                              dynamic scoring),
  * arena_score_events      — append-only ledger: exactly one 'solve' row per
                              accepted solve (audit, value at award time) plus
                              first blood, hint costs, bonuses, penalties and
                              Attack & Defense rows.

Standings are computed with two grouped, indexed queries and cached by
services.scoreboard. They are a pure function of stored rows, so they can be
reproduced and verified at any time (``verify``).
"""

import datetime

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from CTFd.models import Challenges, Users, db
from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.api import ApiError
from CTFd.plugins.arena.models import (
    ArenaEventChallenge,
    ArenaHintUnlock,
    ArenaRegistration,
    ArenaScoreEvent,
    ArenaSolve,
    ArenaTeam,
)
from CTFd.plugins.arena.services import entity as entity_service
from CTFd.plugins.arena.services import settings as event_settings

JEOPARDY_COMPONENTS = ("solve", "first_blood", "hint")
AD_COMPONENTS = ("ad_attack", "ad_defense", "ad_sla")
ADJUSTMENT_COMPONENTS = ("bonus", "penalty")


def _now():
    return datetime.datetime.utcnow()


# ------------------------------------------------------------ ledger writers


def on_solve(event, ec, entity, solve, now):
    """SOLVE_TXN_HOOK: runs inside the solve transaction."""
    db.session.add(
        ArenaScoreEvent(
            event_id=event.id,
            entity_key=entity.key,
            user_id=entity.user_id,
            component="solve",
            points=ec.current_value,
            source="solve",
            source_ref=str(solve.id),
            event_challenge_id=ec.id,
            created_at=now,
        )
    )
    bonus = event_settings.get(event, "first_blood_bonus")
    if bonus and ec.solve_count == 1:
        # We hold the row lock on the event challenge (see submissions), so
        # solve_count == 1 identifies the first solver; the unique source_ref
        # makes a second first-blood row impossible regardless.
        db.session.add(
            ArenaScoreEvent(
                event_id=event.id,
                entity_key=entity.key,
                user_id=entity.user_id,
                component="first_blood",
                points=bonus,
                source="first_blood",
                source_ref=f"ec:{ec.id}",
                event_challenge_id=ec.id,
                created_at=now,
            )
        )
    db.session.flush()


def record_hint_unlock(event, unlock):
    """Called in the hint-unlock transaction (cost as a negative ledger row)."""
    if not unlock.cost:
        return
    db.session.add(
        ArenaScoreEvent(
            event_id=event.id,
            entity_key=unlock.entity_key,
            user_id=unlock.user_id,
            component="hint",
            points=-int(unlock.cost),
            source="hint",
            source_ref=f"{unlock.hint_id}:{unlock.entity_key}",
            event_challenge_id=unlock.event_challenge_id,
            created_at=unlock.created_at or _now(),
        )
    )


def adjust(event, entity_key, points, reason, actor, idempotency_key=None):
    """Organizer bonus/penalty. Idempotent when an idempotency_key is given."""
    try:
        kind, ident = entity_service.parse(entity_key)
    except ValueError:
        raise v.ValidationError("entity_key", "must be t:<team_id> or u:<user_id>")
    if kind == "t":
        if ArenaTeam.query.filter_by(id=ident, event_id=event.id).first() is None:
            raise ApiError(404, "Team not found in this event")
    else:
        if (
            ArenaRegistration.query.filter_by(event_id=event.id, user_id=ident).first()
            is None
        ):
            raise ApiError(404, "Participant not found in this event")
        if event.team_mode == "team":
            raise ApiError(400, "Team events are scored per team (use t:<team_id>)")
    if (
        not isinstance(points, int)
        or isinstance(points, bool)
        or points == 0
        or abs(points) > 100000
    ):
        raise v.ValidationError("points", "must be a non-zero integer within ±100000")
    if not reason or not isinstance(reason, str) or len(reason) > 255:
        raise v.ValidationError("reason", "is required (max 255 characters)")
    ref = idempotency_key or f"{actor.id}:{_now().isoformat()}"
    row = ArenaScoreEvent(
        event_id=event.id,
        entity_key=entity_key,
        component="bonus" if points > 0 else "penalty",
        points=points,
        source="adjustment",
        source_ref=str(ref)[:96],
        reason=reason.strip(),
        actor_user_id=actor.id,
    )
    try:
        db.session.add(row)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError(
            409, "This adjustment was already applied", code="duplicate_adjustment"
        )
    return row


# ------------------------------------------------------------ computations


def _freeze_cutoff(event, scope):
    if scope == "admin" or not event.freeze_at:
        return None
    return event.freeze_at


def _component_weights(event):
    """Phase 10 replaces this with the active scoring configuration."""
    from CTFd.plugins.arena.services import mixed

    return mixed.active_weights(event)


def _raw_totals(event, cutoff, by="entity"):
    """{key: {"components": {...}, "last_at": dt, "solves": n}}"""
    key_col = ArenaSolve.entity_key if by == "entity" else ArenaSolve.user_id
    q = (
        db.session.query(
            key_col,
            func.sum(ArenaEventChallenge.current_value),
            func.count(ArenaSolve.id),
            func.max(ArenaSolve.created_at),
        )
        .join(
            ArenaEventChallenge, ArenaEventChallenge.id == ArenaSolve.event_challenge_id
        )
        .filter(ArenaSolve.event_id == event.id)
    )
    if cutoff:
        q = q.filter(ArenaSolve.created_at < cutoff)
    out = {}
    for key, pts, n, last in q.group_by(key_col).all():
        if key is None:
            continue
        out[key] = {
            "components": {"solve": int(pts or 0)},
            "last_at": last,
            "solves": int(n),
        }

    lkey = ArenaScoreEvent.entity_key if by == "entity" else ArenaScoreEvent.user_id
    lq = db.session.query(
        lkey,
        ArenaScoreEvent.component,
        func.sum(ArenaScoreEvent.points),
        func.max(ArenaScoreEvent.created_at),
    ).filter(ArenaScoreEvent.event_id == event.id, ArenaScoreEvent.component != "solve")
    if by != "entity":
        lq = lq.filter(ArenaScoreEvent.user_id.isnot(None))
    if cutoff:
        lq = lq.filter(ArenaScoreEvent.created_at < cutoff)
    for key, comp, pts, last in lq.group_by(lkey, ArenaScoreEvent.component).all():
        if key is None:
            continue
        row = out.setdefault(key, {"components": {}, "last_at": None, "solves": 0})
        row["components"][comp] = row["components"].get(comp, 0) + int(pts or 0)
        if pts and pts > 0 and (row["last_at"] is None or last > row["last_at"]):
            row["last_at"] = last
    return out


def _names(event, keys, by):
    if by == "entity" and event.team_mode == "team":
        ids = [entity_service.parse(k)[1] for k in keys]
        teams = ArenaTeam.query.filter(
            ArenaTeam.id.in_(ids or [0]), ArenaTeam.event_id == event.id
        )
        return {
            entity_service.team_key(t.id): (t.name, t.status, t.affiliation)
            for t in teams
        }
    ids = [entity_service.parse(k)[1] if isinstance(k, str) else k for k in keys]
    users = Users.query.with_entities(
        Users.id, Users.name, Users.banned, Users.hidden, Users.affiliation
    )
    users = users.filter(Users.id.in_(ids or [0]))
    regs = dict(
        db.session.query(ArenaRegistration.user_id, ArenaRegistration.status).filter(
            ArenaRegistration.event_id == event.id,
            ArenaRegistration.user_id.in_(ids or [0]),
        )
    )
    out = {}
    for uid, name, banned, hidden, aff in users:
        status = "active"
        if banned or hidden or regs.get(uid) != "approved":
            status = "hidden"
        key = entity_service.user_key(uid) if by == "entity" else uid
        out[key] = (name, status, aff)
    return out


def compute_standings(event, scope="public", by="entity"):
    """Ranked list. scope 'public' applies freeze and hides banned/hidden;
    'admin' is live and complete. by='entity' (teams or users) or 'user'."""
    cutoff = _freeze_cutoff(event, scope)
    weights = _component_weights(event)
    raw = _raw_totals(event, cutoff, by=by)
    names = _names(event, list(raw), by)
    rows = []
    for key, data in raw.items():
        name, status, affiliation = names.get(key, (None, "hidden", None))
        if scope != "admin" and status != "active":
            continue
        comps = data["components"]
        jeopardy = sum(comps.get(c, 0) for c in JEOPARDY_COMPONENTS)
        ad = sum(comps.get(c, 0) for c in AD_COMPONENTS)
        adjust_pts = sum(comps.get(c, 0) for c in ADJUSTMENT_COMPONENTS)
        total = round(weights["jeopardy"] * jeopardy + weights["ad"] * ad + adjust_pts)
        rows.append(
            {
                "key": key if by == "entity" else entity_service.user_key(key),
                "name": name,
                "affiliation": affiliation,
                "status": status,
                "score": int(total),
                "breakdown": {
                    "jeopardy": int(jeopardy),
                    "attack_defense": int(ad),
                    "adjustments": int(adjust_pts),
                    "weighted": {
                        "jeopardy": round(weights["jeopardy"] * jeopardy, 2),
                        "attack_defense": round(weights["ad"] * ad, 2),
                    },
                    "weights": weights,
                    "components": {k: int(val) for k, val in sorted(comps.items())},
                },
                "solves": data["solves"],
                "last_score_at": v.iso(data["last_at"]),
                "_last": data["last_at"] or datetime.datetime.max,
            }
        )
    # Stable tie-breaking: score desc, then the configured rule (solves count
    # or A&D attack points), then earliest time the score was reached, then key.
    from CTFd.plugins.arena.services import mixed

    tie_break = mixed.active_config(event)["tie_break"]

    def order(r):
        key = (-r["score"],)
        if tie_break == "solves":
            key += (-r["solves"],)
        elif tie_break == "attack":
            key += (-r["breakdown"]["components"].get("ad_attack", 0),)
        return key + (r["_last"], _key_order(r["key"]))

    rows.sort(key=order)
    rank, prev = 0, None
    for i, r in enumerate(rows, 1):
        r.pop("_last")
        # Ties share no rank: the tie-break above produces a strict order.
        rank = i
        r["rank"] = rank
        prev = r
    del prev
    return rows


def _key_order(key):
    kind, ident = entity_service.parse(key)
    return (kind, ident)


def category_standings(event, scope="public"):
    cutoff = _freeze_cutoff(event, scope)
    q = (
        db.session.query(
            Challenges.category,
            ArenaSolve.entity_key,
            func.sum(ArenaEventChallenge.current_value),
            func.max(ArenaSolve.created_at),
        )
        .join(
            ArenaEventChallenge, ArenaEventChallenge.id == ArenaSolve.event_challenge_id
        )
        .join(Challenges, Challenges.id == ArenaEventChallenge.challenge_id)
        .filter(ArenaSolve.event_id == event.id)
    )
    if cutoff:
        q = q.filter(ArenaSolve.created_at < cutoff)
    by_cat = {}
    rows = q.group_by(Challenges.category, ArenaSolve.entity_key).all()
    names = _names(event, list({r[1] for r in rows}), "entity")
    for cat, key, pts, last in rows:
        name, status, _aff = names.get(key, (None, "hidden", None))
        if scope != "admin" and status != "active":
            continue
        by_cat.setdefault(cat or "Uncategorized", []).append(
            {"key": key, "name": name, "score": int(pts or 0), "_last": last}
        )
    out = {}
    for cat, items in by_cat.items():
        items.sort(key=lambda r: (-r["score"], r["_last"], _key_order(r["key"])))
        for i, r in enumerate(items, 1):
            r.pop("_last")
            r["rank"] = i
        out[cat] = items[:50]
    return out


def history(event, keys, scope="public"):
    """Cumulative score series for the given entity keys."""
    cutoff = _freeze_cutoff(event, scope)
    weights = _component_weights(event)
    q = (
        db.session.query(
            ArenaSolve.entity_key,
            ArenaSolve.created_at,
            ArenaEventChallenge.current_value,
        )
        .join(
            ArenaEventChallenge, ArenaEventChallenge.id == ArenaSolve.event_challenge_id
        )
        .filter(
            ArenaSolve.event_id == event.id, ArenaSolve.entity_key.in_(keys or [""])
        )
    )
    lq = db.session.query(
        ArenaScoreEvent.entity_key,
        ArenaScoreEvent.created_at,
        ArenaScoreEvent.points,
        ArenaScoreEvent.component,
    ).filter(
        ArenaScoreEvent.event_id == event.id,
        ArenaScoreEvent.entity_key.in_(keys or [""]),
        ArenaScoreEvent.component != "solve",
    )
    if cutoff:
        q = q.filter(ArenaSolve.created_at < cutoff)
        lq = lq.filter(ArenaScoreEvent.created_at < cutoff)
    points = [(k, t, p * weights["jeopardy"]) for k, t, p in q.all()]
    for k, t, p, comp in lq.all():
        w = (
            weights["ad"]
            if comp in AD_COMPONENTS
            else (1 if comp in ADJUSTMENT_COMPONENTS else weights["jeopardy"])
        )
        points.append((k, t, p * w))
    points.sort(key=lambda x: x[1])
    series = {k: [] for k in keys}
    totals = dict.fromkeys(keys, 0)
    for k, t, p in points:
        totals[k] += p
        series[k].append([v.iso(t), round(totals[k])])
    return series


def challenge_stats(event):
    rows = (
        db.session.query(ArenaEventChallenge, Challenges.name, Challenges.category)
        .join(Challenges, Challenges.id == ArenaEventChallenge.challenge_id)
        .filter(ArenaEventChallenge.event_id == event.id)
        .all()
    )
    firsts = dict(
        db.session.query(
            ArenaScoreEvent.event_challenge_id, ArenaScoreEvent.entity_key
        ).filter(
            ArenaScoreEvent.event_id == event.id,
            ArenaScoreEvent.component == "first_blood",
        )
    )
    entities = (
        db.session.query(func.count(func.distinct(ArenaSolve.entity_key)))
        .filter(ArenaSolve.event_id == event.id)
        .scalar()
        or 0
    )
    return [
        {
            "id": ec.id,
            "name": name,
            "category": cat,
            "value": ec.current_value,
            "solves": ec.solve_count,
            "first_blood": firsts.get(ec.id),
            "solve_rate": round(ec.solve_count / entities, 4) if entities else 0.0,
        }
        for ec, name, cat in rows
    ]


def entity_score(event, key, scope="public"):
    for row in compute_standings(event, scope=scope):
        if row["key"] == key:
            return row
    return None


# ------------------------------------------------------------------ verify


def verify(event):
    """Recompute from source rows and check ledger invariants."""
    problems = []
    solves = ArenaSolve.query.filter_by(event_id=event.id).count()
    solve_rows = ArenaScoreEvent.query.filter_by(
        event_id=event.id, component="solve"
    ).count()
    if solves != solve_rows:
        problems.append(f"{solves} solves but {solve_rows} 'solve' ledger rows")
    dup = (
        db.session.query(
            ArenaSolve.event_challenge_id,
            ArenaSolve.entity_key,
            func.count(ArenaSolve.id),
        )
        .filter(ArenaSolve.event_id == event.id)
        .group_by(ArenaSolve.event_challenge_id, ArenaSolve.entity_key)
        .having(func.count(ArenaSolve.id) > 1)
        .count()
    )
    if dup:
        problems.append(f"{dup} duplicate solves")
    # Compared in Python: a SQL CAST(id AS CHAR) = source_ref comparison
    # depends on the connection collation and fails on MariaDB servers whose
    # connection default (utf8mb4_general_ci) differs from the columns'.
    solve_ids = {
        str(i)
        for (i,) in db.session.query(ArenaSolve.id).filter(
            ArenaSolve.event_id == event.id
        )
    }
    orphan = sum(
        1
        for (ref,) in db.session.query(ArenaScoreEvent.source_ref).filter(
            ArenaScoreEvent.event_id == event.id,
            ArenaScoreEvent.component == "solve",
        )
        if ref not in solve_ids
    )
    if orphan:
        problems.append(f"{orphan} 'solve' ledger rows without a solve")
    unlocks = ArenaHintUnlock.query.filter(
        ArenaHintUnlock.event_id == event.id, ArenaHintUnlock.cost > 0
    ).count()
    hint_rows = ArenaScoreEvent.query.filter_by(
        event_id=event.id, component="hint"
    ).count()
    if unlocks != hint_rows:
        problems.append(f"{unlocks} paid hint unlocks but {hint_rows} hint ledger rows")
    return problems


def serialize_event_row(row):
    return {
        "id": row.id,
        "entity_key": row.entity_key,
        "user_id": row.user_id,
        "component": row.component,
        "points": row.points,
        "source": row.source,
        "source_ref": row.source_ref,
        "challenge_id": row.event_challenge_id,
        "reason": row.reason,
        "actor_user_id": row.actor_user_id,
        "config_version": row.config_version,
        "created_at": v.iso(row.created_at),
    }

"""Competition analytics (Phase 7).

Expensive aggregation happens only in worker jobs (``analytics.rollup``),
which recompute recent buckets idempotently (delete + insert per bucket).
Dashboard endpoints read the rollup tables plus a few indexed COUNTs.
"""

import csv
import datetime
import io
import statistics

from sqlalchemy import func

from CTFd.models import Challenges, Users, db
from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.models import (
    ArenaActivity,
    ArenaEventChallenge,
    ArenaRegistration,
    ArenaSolve,
    ArenaStatsChallenge,
    ArenaStatsHourly,
    ArenaSubmission,
    ArenaTeam,
)


def _hour(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def rollup_hourly(event, start=None, end=None):
    end = end or datetime.datetime.utcnow()
    start = _hour(start or (end - datetime.timedelta(hours=3)))
    buckets = 0
    cur = start
    while cur <= end:
        nxt = cur + datetime.timedelta(hours=1)
        subs = (
            db.session.query(
                func.count(ArenaSubmission.id),
                func.count(func.distinct(ArenaSubmission.entity_key)),
            )
            .filter(
                ArenaSubmission.event_id == event.id,
                ArenaSubmission.created_at >= cur,
                ArenaSubmission.created_at < nxt,
            )
            .one()
        )
        correct = (
            db.session.query(func.count(ArenaSolve.id))
            .filter(
                ArenaSolve.event_id == event.id,
                ArenaSolve.created_at >= cur,
                ArenaSolve.created_at < nxt,
            )
            .scalar()
        )
        users = {
            u
            for (u,) in db.session.query(ArenaSubmission.user_id)
            .filter(
                ArenaSubmission.event_id == event.id,
                ArenaSubmission.created_at >= cur,
                ArenaSubmission.created_at < nxt,
            )
            .distinct()
            if u
        }
        users |= {
            u
            for (u,) in db.session.query(ArenaActivity.actor_user_id)
            .filter(
                ArenaActivity.event_id == event.id,
                ArenaActivity.actor_type == "user",
                ArenaActivity.created_at >= cur,
                ArenaActivity.created_at < nxt,
            )
            .distinct()
            if u
        }
        regs = (
            db.session.query(func.count(ArenaRegistration.id))
            .filter(
                ArenaRegistration.event_id == event.id,
                ArenaRegistration.created_at >= cur,
                ArenaRegistration.created_at < nxt,
            )
            .scalar()
        )
        ArenaStatsHourly.query.filter_by(event_id=event.id, bucket_start=cur).delete()
        if subs[0] or correct or users or regs:
            db.session.add(
                ArenaStatsHourly(
                    event_id=event.id,
                    bucket_start=cur,
                    active_users=len(users),
                    active_entities=subs[1],
                    submissions=subs[0],
                    correct=correct,
                    registrations=regs,
                    updated_at=datetime.datetime.utcnow(),
                )
            )
        buckets += 1
        cur = nxt
    db.session.commit()
    return buckets


def rollup_challenges(event):
    start = event.starts_at
    agg = {
        ec_id: (n, inc, uniq)
        for ec_id, n, inc, uniq in db.session.query(
            ArenaSubmission.event_challenge_id,
            func.count(ArenaSubmission.id),
            func.sum(db.case((ArenaSubmission.status == "incorrect", 1), else_=0)),
            func.count(func.distinct(ArenaSubmission.entity_key)),
        )
        .filter(ArenaSubmission.event_id == event.id)
        .group_by(ArenaSubmission.event_challenge_id)
    }
    solve_times = {}
    for ec_id, created in db.session.query(
        ArenaSolve.event_challenge_id, ArenaSolve.created_at
    ).filter(ArenaSolve.event_id == event.id):
        solve_times.setdefault(ec_id, []).append(created)
    ArenaStatsChallenge.query.filter_by(event_id=event.id).delete()
    for ec in ArenaEventChallenge.query.filter_by(event_id=event.id):
        n, inc, uniq = agg.get(ec.id, (0, 0, 0))
        times = sorted(solve_times.get(ec.id, []))
        median = None
        if times and start:
            median = int(statistics.median((t - start).total_seconds() for t in times))
        db.session.add(
            ArenaStatsChallenge(
                event_id=event.id,
                event_challenge_id=ec.id,
                attempts=int(n or 0),
                incorrect=int(inc or 0),
                solves=len(times),
                unique_attempters=int(uniq or 0),
                first_solve_at=times[0] if times else None,
                median_solve_seconds=median,
                updated_at=datetime.datetime.utcnow(),
            )
        )
    db.session.commit()


def overview(event):
    regs = dict(
        db.session.query(ArenaRegistration.status, func.count(ArenaRegistration.id))
        .filter(ArenaRegistration.event_id == event.id)
        .group_by(ArenaRegistration.status)
    )
    teams = dict(
        db.session.query(ArenaTeam.status, func.count(ArenaTeam.id))
        .filter(ArenaTeam.event_id == event.id)
        .group_by(ArenaTeam.status)
    )
    totals = (
        db.session.query(
            func.coalesce(func.sum(ArenaStatsHourly.submissions), 0),
            func.coalesce(func.sum(ArenaStatsHourly.correct), 0),
        )
        .filter(ArenaStatsHourly.event_id == event.id)
        .one()
    )
    last_hour = (
        ArenaStatsHourly.query.filter_by(event_id=event.id)
        .order_by(ArenaStatsHourly.bucket_start.desc())
        .first()
    )
    per_entity = dict(
        db.session.query(ArenaSolve.entity_key, func.count(ArenaSolve.id))
        .filter(ArenaSolve.event_id == event.id)
        .group_by(ArenaSolve.entity_key)
    )
    visible = ArenaEventChallenge.query.filter_by(
        event_id=event.id, visible=True
    ).count()
    subs, correct = int(totals[0]), int(totals[1])
    return {
        "registrations": {"total": sum(regs.values()), "by_status": regs},
        "teams": {"total": sum(teams.values()), "by_status": teams},
        "submissions": subs,
        "correct_submissions": correct,
        "failed_submissions": subs - correct,
        "success_rate": round(correct / subs, 4) if subs else 0.0,
        "active_users_last_hour": last_hour.active_users if last_hour else 0,
        "completion": {
            "entities_with_solves": len(per_entity),
            "entities_completed_all": sum(
                1 for n in per_entity.values() if visible and n >= visible
            ),
            "avg_solves_per_entity": round(
                sum(per_entity.values()) / len(per_entity), 2
            )
            if per_entity
            else 0.0,
            "visible_challenges": visible,
        },
        "rollup_updated_at": v.iso(last_hour.updated_at) if last_hour else None,
    }


def timeline(event, hours=48):
    since = _hour(datetime.datetime.utcnow() - datetime.timedelta(hours=hours))
    rows = ArenaStatsHourly.query.filter(
        ArenaStatsHourly.event_id == event.id, ArenaStatsHourly.bucket_start >= since
    ).order_by(ArenaStatsHourly.bucket_start)
    return [
        {
            "bucket": v.iso(r.bucket_start),
            "active_users": r.active_users,
            "active_entities": r.active_entities,
            "submissions": r.submissions,
            "correct": r.correct,
            "registrations": r.registrations,
        }
        for r in rows
    ]


def challenge_report(event):
    rows = (
        db.session.query(
            ArenaStatsChallenge,
            ArenaEventChallenge,
            Challenges.name,
            Challenges.category,
        )
        .join(
            ArenaEventChallenge,
            ArenaEventChallenge.id == ArenaStatsChallenge.event_challenge_id,
        )
        .join(Challenges, Challenges.id == ArenaEventChallenge.challenge_id)
        .filter(ArenaStatsChallenge.event_id == event.id)
        .all()
    )
    active = (
        db.session.query(func.count(func.distinct(ArenaSubmission.entity_key)))
        .filter(ArenaSubmission.event_id == event.id)
        .scalar()
        or 0
    )
    out = []
    for st, ec, name, cat in rows:
        out.append(
            {
                "id": ec.id,
                "name": name,
                "category": cat,
                "difficulty": ec.difficulty,
                "value": ec.current_value,
                "attempts": st.attempts,
                "incorrect": st.incorrect,
                "solves": st.solves,
                "unique_attempters": st.unique_attempters,
                "solve_rate": round(st.solves / active, 4) if active else 0.0,
                "success_rate": round(st.solves / st.attempts, 4)
                if st.attempts
                else 0.0,
                "first_solve_at": v.iso(st.first_solve_at),
                "median_solve_seconds": st.median_solve_seconds,
            }
        )
    return sorted(out, key=lambda r: (-r["solves"], r["name"] or ""))


# -------------------------------------------------------------------- CSV

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _cell(value):
    """CSV-injection safe cell (spreadsheet formulas are neutralised)."""
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        return v.iso(value)
    text = str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def stream_csv(header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    yield buf.getvalue()
    for row in rows:
        buf.seek(0)
        buf.truncate(0)
        writer.writerow([_cell(c) for c in row])
        yield buf.getvalue()


def export_rows(event, kind, limit=100000):
    """(header, iterator) for a CSV export; iterates in bounded chunks."""

    def chunked(query, key_col, fn, size=1000):
        last = 0
        sent = 0
        while sent < limit:
            batch = query.filter(key_col > last).order_by(key_col).limit(size).all()
            if not batch:
                return
            for row in batch:
                yield fn(row)
                sent += 1
            last = getattr(batch[-1], "id", None) or batch[-1][0].id

    names = dict(
        Users.query.with_entities(Users.id, Users.name)
        .join(ArenaRegistration, ArenaRegistration.user_id == Users.id)
        .filter(ArenaRegistration.event_id == event.id)
    )
    if kind == "registrations":
        q = ArenaRegistration.query.filter_by(event_id=event.id)
        return (
            ["id", "user_id", "user", "status", "source", "created_at"],
            chunked(
                q,
                ArenaRegistration.id,
                lambda r: [
                    r.id,
                    r.user_id,
                    names.get(r.user_id),
                    r.status,
                    r.source,
                    r.created_at,
                ],
            ),
        )
    if kind == "teams":
        q = ArenaTeam.query.filter_by(event_id=event.id)
        return (
            [
                "id",
                "name",
                "status",
                "visibility",
                "members",
                "captain_user_id",
                "created_at",
            ],
            chunked(
                q,
                ArenaTeam.id,
                lambda t: [
                    t.id,
                    t.name,
                    t.status,
                    t.visibility,
                    t.member_count,
                    t.captain_user_id,
                    t.created_at,
                ],
            ),
        )
    if kind == "submissions":
        q = ArenaSubmission.query.filter_by(event_id=event.id)
        return (
            ["id", "challenge_id", "entity", "user_id", "status", "created_at"],
            chunked(
                q,
                ArenaSubmission.id,
                lambda s: [
                    s.id,
                    s.event_challenge_id,
                    s.entity_key,
                    s.user_id,
                    s.status,
                    s.created_at,
                ],
            ),
        )
    if kind == "solves":
        q = ArenaSolve.query.filter_by(event_id=event.id)
        return (
            ["id", "challenge_id", "entity", "user_id", "created_at"],
            chunked(
                q,
                ArenaSolve.id,
                lambda s: [
                    s.id,
                    s.event_challenge_id,
                    s.entity_key,
                    s.user_id,
                    s.created_at,
                ],
            ),
        )
    if kind == "activity":
        q = ArenaActivity.query.filter_by(event_id=event.id)
        return (
            [
                "id",
                "actor_user_id",
                "actor_type",
                "action",
                "target_type",
                "target_id",
                "created_at",
            ],
            chunked(
                q,
                ArenaActivity.id,
                lambda a: [
                    a.id,
                    a.actor_user_id,
                    a.actor_type,
                    a.action,
                    a.target_type,
                    a.target_id,
                    a.created_at,
                ],
            ),
        )
    if kind == "challenges":
        rows = challenge_report(event)
        cols = [
            "id",
            "name",
            "category",
            "difficulty",
            "value",
            "attempts",
            "incorrect",
            "solves",
            "unique_attempters",
            "solve_rate",
            "success_rate",
            "first_solve_at",
            "median_solve_seconds",
        ]
        return cols, ([r[c] for c in cols] for r in rows)
    if kind == "scoreboard":
        from CTFd.plugins.arena.services import scoring

        rows = scoring.compute_standings(event, scope="admin")
        return (
            [
                "rank",
                "key",
                "name",
                "score",
                "jeopardy",
                "attack_defense",
                "adjustments",
                "solves",
                "last_score_at",
                "status",
            ],
            (
                [
                    r["rank"],
                    r["key"],
                    r["name"],
                    r["score"],
                    r["breakdown"]["jeopardy"],
                    r["breakdown"]["attack_defense"],
                    r["breakdown"]["adjustments"],
                    r["solves"],
                    r["last_score_at"],
                    r["status"],
                ]
                for r in rows
            ),
        )
    raise KeyError(kind)


EXPORT_KINDS = (
    "registrations",
    "teams",
    "submissions",
    "solves",
    "activity",
    "challenges",
    "scoreboard",
)

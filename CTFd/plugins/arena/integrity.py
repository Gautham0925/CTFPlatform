"""Database integrity checks for Arena (``flask arena check-integrity``).

Each check returns a list of human-readable problems. An empty list is a pass.
Checks are read-only.
"""

from sqlalchemy import func

from CTFd.models import ModuleAudienceAccess, Modules, Users, db
from CTFd.plugins.arena import dbmigrate
from CTFd.plugins.arena.models import ArenaEvent, ArenaRole


def check_schema():
    ok, detail = dbmigrate.schema_state()
    return [] if ok else [f"schema not at head: {detail}"]


def check_event_windows():
    problems = []
    for e in ArenaEvent.query.all():
        if e.starts_at and e.ends_at and e.starts_at >= e.ends_at:
            problems.append(f"event {e.id}: starts_at >= ends_at")
        if e.freeze_at and e.starts_at and e.freeze_at < e.starts_at:
            problems.append(f"event {e.id}: freeze_at before starts_at")
        if e.freeze_at and e.ends_at and e.freeze_at > e.ends_at:
            problems.append(f"event {e.id}: freeze_at after ends_at")
        if e.team_size_min < 1 or e.team_size_max < e.team_size_min:
            problems.append(f"event {e.id}: invalid team size bounds")
        if e.max_participants is not None and e.max_participants < 1:
            problems.append(f"event {e.id}: max_participants < 1")
    return problems


def check_sealed_modules():
    """A sealed module must exist and must never be given an audience."""
    problems = []
    rows = (
        db.session.query(ArenaEvent.id, ArenaEvent.sealed_module_id)
        .filter(ArenaEvent.sealed_module_id.isnot(None))
        .all()
    )
    for event_id, module_id in rows:
        if Modules.query.filter_by(id=module_id).first() is None:
            problems.append(f"event {event_id}: sealed module {module_id} missing")
            continue
        audiences = (
            db.session.query(func.count(ModuleAudienceAccess.id))
            .filter(ModuleAudienceAccess.module_id == module_id)
            .scalar()
        )
        if audiences:
            problems.append(
                f"event {event_id}: sealed module {module_id} has {audiences} "
                "audience link(s) - challenges are exposed to native CTFd"
            )
    return problems


def check_roles():
    problems = []
    dupes = (
        db.session.query(ArenaRole.user_id, ArenaRole.role, func.count(ArenaRole.id))
        .filter(ArenaRole.event_id.is_(None))
        .group_by(ArenaRole.user_id, ArenaRole.role)
        .having(func.count(ArenaRole.id) > 1)
        .all()
    )
    for user_id, role, n in dupes:
        problems.append(f"user {user_id}: global role {role} granted {n} times")
    admin_roles = (
        db.session.query(ArenaRole.id)
        .join(Users, Users.id == ArenaRole.user_id)
        .filter(Users.type == "admin")
        .count()
    )
    if admin_roles:
        # Not harmful, but redundant: superadmins have every permission.
        problems.append(
            f"{admin_roles} role row(s) granted to native admins (redundant)"
        )
    return problems


def check_seat_counts():
    from CTFd.plugins.arena.models import SEAT_STATUSES, ArenaRegistration

    problems = []
    held = dict(
        db.session.query(ArenaRegistration.event_id, func.count(ArenaRegistration.id))
        .filter(ArenaRegistration.status.in_(SEAT_STATUSES))
        .group_by(ArenaRegistration.event_id)
        .all()
    )
    for e in ArenaEvent.query.all():
        actual = held.get(e.id, 0)
        if e.seat_count != actual:
            problems.append(
                f"event {e.id}: seat_count={e.seat_count} but {actual} seats held"
            )
        if e.max_participants is not None and actual > e.max_participants:
            problems.append(
                f"event {e.id}: {actual} seats held exceeds max_participants={e.max_participants}"
            )
    return problems


def check_teams():
    from CTFd.plugins.arena.models import (
        ArenaRegistration,
        ArenaTeam,
        ArenaTeamMember,
    )

    problems = []
    counts = dict(
        db.session.query(ArenaTeamMember.team_id, func.count(ArenaTeamMember.id))
        .group_by(ArenaTeamMember.team_id)
        .all()
    )
    for t in ArenaTeam.query.all():
        n = counts.get(t.id, 0)
        if t.member_count != n:
            problems.append(
                f"team {t.id}: member_count={t.member_count} but {n} members"
            )
        captains = ArenaTeamMember.query.filter_by(team_id=t.id, role="captain").all()
        if n and (len(captains) != 1 or captains[0].user_id != t.captain_user_id):
            problems.append(f"team {t.id}: captain inconsistent")
    mismatched = (
        db.session.query(ArenaTeamMember.id)
        .join(ArenaTeam, ArenaTeam.id == ArenaTeamMember.team_id)
        .filter(ArenaTeam.event_id != ArenaTeamMember.event_id)
        .count()
    )
    if mismatched:
        problems.append(f"{mismatched} membership(s) with event_id != team.event_id")
    unregistered = (
        db.session.query(ArenaTeamMember.id)
        .outerjoin(
            ArenaRegistration,
            (ArenaRegistration.event_id == ArenaTeamMember.event_id)
            & (ArenaRegistration.user_id == ArenaTeamMember.user_id)
            & (ArenaRegistration.status == "approved"),
        )
        .filter(ArenaRegistration.id.is_(None))
        .count()
    )
    if unregistered:
        problems.append(f"{unregistered} team member(s) without approved registration")
    return problems


def check_event_challenges():
    """Every event challenge must stay sealed (native endpoints must 404)."""
    from CTFd.models import Challenges
    from CTFd.plugins.arena.models import ArenaEventChallenge, ArenaSolve

    problems = []
    rows = (
        db.session.query(ArenaEventChallenge, Challenges, ArenaEvent.sealed_module_id)
        .join(Challenges, Challenges.id == ArenaEventChallenge.challenge_id)
        .join(ArenaEvent, ArenaEvent.id == ArenaEventChallenge.event_id)
        .all()
    )
    for ec, c, sealed in rows:
        if sealed is None or c.module_id is None:
            problems.append(
                f"event challenge {ec.id}: NOT SEALED (event has no sealed module)"
            )
        elif c.module_id != sealed:
            problems.append(
                f"event challenge {ec.id}: native module_id={c.module_id} != sealed {sealed}"
            )
        if c.state != "hidden":
            problems.append(f"event challenge {ec.id}: native state is {c.state!r}")
    counts = dict(
        db.session.query(ArenaSolve.event_challenge_id, func.count(ArenaSolve.id))
        .group_by(ArenaSolve.event_challenge_id)
        .all()
    )
    for ec, _c, _s in rows:
        if ec.solve_count != counts.get(ec.id, 0):
            problems.append(f"event challenge {ec.id}: solve_count mismatch")
    return problems


def check_scoring():
    from CTFd.plugins.arena.services import scoring

    problems = []
    for e in ArenaEvent.query.all():
        problems += [f"event {e.id}: {p}" for p in scoring.verify(e)]
    return problems


CHECKS = {
    "schema": check_schema,
    "event_windows": check_event_windows,
    "sealed_modules": check_sealed_modules,
    "roles": check_roles,
    "seat_counts": check_seat_counts,
    "teams": check_teams,
    "event_challenges": check_event_challenges,
    "scoring": check_scoring,
}


def run_all():
    results = {}
    for name, fn in CHECKS.items():
        if name != "schema" and check_schema():
            results[name] = ["skipped: schema not ready"]
            continue
        results[name] = fn()
    return results

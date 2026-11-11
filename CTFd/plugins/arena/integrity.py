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


CHECKS = {
    "schema": check_schema,
    "event_windows": check_event_windows,
    "sealed_modules": check_sealed_modules,
    "roles": check_roles,
}


def run_all():
    results = {}
    for name, fn in CHECKS.items():
        if name != "schema" and check_schema():
            results[name] = ["skipped: schema not ready"]
            continue
        results[name] = fn()
    return results

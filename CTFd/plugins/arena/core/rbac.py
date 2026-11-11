"""Role-based access control for Arena administration.

Native CTFd admins (``Users.type == "admin"``) are superadmins. Every other
grant is an ``arena_roles`` row, scoped to one event or (event_id NULL) all
events. Permissions are checked against the database on every call, never
against session flags.

Some permissions belong to no role and are reserved for superadmins: creating
events and granting roles. A role holder therefore cannot escalate privileges
by granting themselves or others new roles.
"""

from sqlalchemy import or_

from CTFd.models import db
from CTFd.plugins.arena.models import (
    ROLE_AD_OPERATOR,
    ROLE_ANALYST,
    ROLE_CERT_MANAGER,
    ROLE_CHALLENGE_MANAGER,
    ROLE_ORGANIZER,
    ROLE_SCOREBOARD_OPERATOR,
    ROLES,
    ArenaRole,
)

PERM_EVENT_VIEW_ADMIN = "event.view_admin"
PERM_EVENT_MANAGE = "event.manage"
PERM_REGISTRATIONS_MANAGE = "registrations.manage"
PERM_TEAMS_MANAGE = "teams.manage"
PERM_CHALLENGES_MANAGE = "challenges.manage"
PERM_SCORING_MANAGE = "scoring.manage"
PERM_AD_OPERATE = "ad.operate"
PERM_LABS_MANAGE = "labs.manage"
PERM_CERTIFICATES_MANAGE = "certificates.manage"
PERM_AUDIT_READ = "audit.read"
PERM_ANALYTICS_READ = "analytics.read"
# Superadmin-only (granted by no role):
PERM_EVENTS_CREATE = "events.create"
PERM_ROLES_MANAGE = "roles.manage"
PERM_PLATFORM_MANAGE = "platform.manage"  # runner/providers/jobs, platform-wide

ROLE_PERMISSIONS = {
    # Event organizer: full control of one event (not platform-wide ops).
    ROLE_ORGANIZER: frozenset(
        {
            PERM_EVENT_VIEW_ADMIN,
            PERM_EVENT_MANAGE,
            PERM_REGISTRATIONS_MANAGE,
            PERM_TEAMS_MANAGE,
            PERM_CHALLENGES_MANAGE,
            PERM_SCORING_MANAGE,
            PERM_AD_OPERATE,
            PERM_LABS_MANAGE,
            PERM_CERTIFICATES_MANAGE,
            PERM_AUDIT_READ,
            PERM_ANALYTICS_READ,
        }
    ),
    ROLE_CHALLENGE_MANAGER: frozenset({PERM_EVENT_VIEW_ADMIN, PERM_CHALLENGES_MANAGE}),
    ROLE_SCOREBOARD_OPERATOR: frozenset({PERM_EVENT_VIEW_ADMIN, PERM_SCORING_MANAGE}),
    ROLE_AD_OPERATOR: frozenset({PERM_EVENT_VIEW_ADMIN, PERM_AD_OPERATE}),
    ROLE_CERT_MANAGER: frozenset({PERM_EVENT_VIEW_ADMIN, PERM_CERTIFICATES_MANAGE}),
    # Read-only analyst.
    ROLE_ANALYST: frozenset(
        {PERM_EVENT_VIEW_ADMIN, PERM_AUDIT_READ, PERM_ANALYTICS_READ}
    ),
}

ALL_PERMISSIONS = frozenset().union(*ROLE_PERMISSIONS.values()) | {
    PERM_EVENTS_CREATE,
    PERM_ROLES_MANAGE,
    PERM_PLATFORM_MANAGE,
}
SUPERADMIN_ONLY = frozenset(
    {PERM_EVENTS_CREATE, PERM_ROLES_MANAGE, PERM_PLATFORM_MANAGE}
)

if set(ROLE_PERMISSIONS) != set(ROLES):  # explicit: asserts vanish under -O
    raise RuntimeError("ROLE_PERMISSIONS must define every role")


class UnknownPermission(ValueError):
    pass


def is_superadmin(user):
    return bool(user is not None and user.type == "admin" and not user.banned)


def roles_for(user, event_id):
    """Roles the user holds for ``event_id`` (including global roles)."""
    if user is None or user.banned:
        return set()
    q = ArenaRole.query.with_entities(ArenaRole.role).filter(
        ArenaRole.user_id == user.id
    )
    if event_id is None:
        q = q.filter(ArenaRole.event_id.is_(None))
    else:
        q = q.filter(or_(ArenaRole.event_id == event_id, ArenaRole.event_id.is_(None)))
    return {role for (role,) in q.all()}


def has_permission(user, permission, event_id=None):
    if permission not in ALL_PERMISSIONS:
        # Fail closed on typos instead of silently denying/allowing.
        raise UnknownPermission(permission)
    if is_superadmin(user):
        return True
    for role in roles_for(user, event_id):
        if permission in ROLE_PERMISSIONS.get(role, ()):
            return True
    return False


def has_any_role(user, event_id):
    return is_superadmin(user) or bool(roles_for(user, event_id))


def grant_role(user_id, role, event_id=None, granted_by=None):
    """Idempotently grant a role. Callers must check PERM_ROLES_MANAGE first."""
    if role not in ROLES:
        raise ValueError(f"Invalid role: {role!r}")
    existing = ArenaRole.query.filter_by(
        user_id=user_id, event_id=event_id, role=role
    ).first()
    if existing:
        return existing
    grant = ArenaRole(
        user_id=user_id, event_id=event_id, role=role, granted_by=granted_by
    )
    db.session.add(grant)
    db.session.commit()
    return grant


def revoke_role(user_id, role, event_id=None):
    deleted = ArenaRole.query.filter_by(
        user_id=user_id, event_id=event_id, role=role
    ).delete()
    db.session.commit()
    return deleted

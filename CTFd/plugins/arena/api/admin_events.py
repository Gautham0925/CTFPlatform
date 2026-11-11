"""Organizer/admin event management API (/api/v1/arena/admin)."""

from flask import request

from CTFd.models import Users
from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.api import (
    API_PREFIX,
    ApiError,
    api_blueprint,
    json_body,
    ok,
    paginate,
    pagination,
)
from CTFd.plugins.arena.core import rbac
from CTFd.plugins.arena.core.authz import (
    event_permission_required,
    global_permission_required,
)
from CTFd.plugins.arena.core.context import scoped_get_or_404, scoped_query
from CTFd.plugins.arena.models import (
    REGISTRATION_STATUSES,
    ROLES,
    ArenaEvent,
    ArenaEventInvite,
    ArenaRegistration,
    ArenaRole,
)
from CTFd.plugins.arena.services import events as event_service
from CTFd.plugins.arena.services import registration as reg_service
from CTFd.plugins.arena.services import scoreboard
from CTFd.plugins.arena.services.audit import audit
from CTFd.utils.user import get_current_user

admin_events_api = api_blueprint("arena_admin_events_api", __name__)
P = f"{API_PREFIX}/admin"


@admin_events_api.route(f"{P}/events", methods=["POST"])
@global_permission_required(rbac.PERM_EVENTS_CREATE)
def create_event():
    actor = get_current_user()
    event = event_service.create_event(json_body(), actor)
    audit("event.create", actor=actor, event_id=event.id, target=("event", event.id))
    return ok(event_service.serialize_admin(event), status=201)


@admin_events_api.route(f"{P}/events")
def list_admin_events():
    from CTFd.utils.user import authed

    if not authed():
        raise ApiError(401)
    user = get_current_user()
    q = ArenaEvent.query
    if not rbac.is_superadmin(user):
        rows = ArenaRole.query.with_entities(ArenaRole.event_id).filter_by(
            user_id=user.id
        )
        ids = {eid for (eid,) in rows}
        if not ids:
            raise ApiError(403)
        if None not in ids:
            q = q.filter(ArenaEvent.id.in_(ids))
    status = request.args.get("status")
    if status:
        q = q.filter(ArenaEvent.status == status)
    page, per_page = pagination()
    data, meta = paginate(
        q.order_by(ArenaEvent.id.desc()), page, per_page, event_service.serialize_admin
    )
    return ok(data, meta=meta)


@admin_events_api.route(f"{P}/events/<int:event_id>")
@event_permission_required(rbac.PERM_EVENT_VIEW_ADMIN)
def get_admin_event(event):
    data = event_service.serialize_admin(event)
    data["my_permissions"] = sorted(
        p
        for p in rbac.ALL_PERMISSIONS
        if p not in rbac.SUPERADMIN_ONLY
        and rbac.has_permission(get_current_user(), p, event.id)
    )
    return ok(data)


@admin_events_api.route(f"{P}/events/<int:event_id>", methods=["PATCH"])
@event_permission_required(rbac.PERM_EVENT_MANAGE)
def update_event(event):
    data = json_body()
    if "status" in data:
        raise ApiError(400, "Use the /status endpoint to change status")
    event_service.update_event(event, data)
    audit(
        "event.update",
        event_id=event.id,
        target=("event", event.id),
        meta={"fields": sorted(data)},
    )
    return ok(event_service.serialize_admin(event))


@admin_events_api.route(f"{P}/events/<int:event_id>/status", methods=["POST"])
@event_permission_required(rbac.PERM_EVENT_MANAGE)
def set_status(event):
    data = json_body()
    to = v.string(data, "status", required=True, max_len=16)
    old = event.status
    event_service.transition(event, to)
    audit(
        "event.status",
        event_id=event.id,
        target=("event", event.id),
        meta={"from": old, "to": to},
    )
    return ok(event_service.serialize_admin(event))


@admin_events_api.route(f"{P}/events/<int:event_id>", methods=["DELETE"])
@event_permission_required(rbac.PERM_EVENT_MANAGE)
def delete_event(event):
    eid = event.id
    event_service.delete_event(event)
    audit("event.delete", event_id=None, target=("event", eid))
    return ok({"deleted": eid})


# ------------------------------------------------------------ registrations


@admin_events_api.route(f"{P}/events/<int:event_id>/registrations")
@event_permission_required(rbac.PERM_EVENT_VIEW_ADMIN)
def list_registrations(event):
    q = scoped_query(ArenaRegistration, event)
    status = request.args.get("status")
    if status:
        if status not in REGISTRATION_STATUSES:
            raise ApiError(400, "Unknown status")
        q = q.filter(ArenaRegistration.status == status)
    page, per_page = pagination()
    data, meta = paginate(
        q.order_by(ArenaRegistration.id),
        page,
        per_page,
        lambda r: reg_service.serialize(r, include_user=True),
    )
    return ok(data, meta=meta)


@admin_events_api.route(f"{P}/events/<int:event_id>/registrations", methods=["POST"])
@event_permission_required(rbac.PERM_REGISTRATIONS_MANAGE)
def enroll(event):
    data = json_body()
    user_id = v.integer(data, "user_id", required=True, minimum=1)
    force = bool(v.boolean(data, "force"))
    reg = reg_service.admin_enroll(event, user_id, get_current_user(), force=force)
    audit(
        "registration.enroll",
        event_id=event.id,
        target=("user", user_id),
        meta={"force": force},
    )
    return ok(reg_service.serialize(reg), status=201)


@admin_events_api.route(
    f"{P}/events/<int:event_id>/registrations/<int:reg_id>/<action>", methods=["POST"]
)
@event_permission_required(rbac.PERM_REGISTRATIONS_MANAGE)
def registration_action(event, reg_id, action):
    reg = scoped_get_or_404(ArenaRegistration, reg_id, event)
    reg_service.admin_action(event, reg, action, get_current_user())
    scoreboard.invalidate(event.id)
    audit(f"registration.{action}", event_id=event.id, target=("user", reg.user_id))
    return ok(reg_service.serialize(reg))


# ------------------------------------------------------------------ invites


@admin_events_api.route(f"{P}/events/<int:event_id>/invites")
@event_permission_required(rbac.PERM_REGISTRATIONS_MANAGE)
def list_invites(event):
    invites = scoped_query(ArenaEventInvite, event).order_by(ArenaEventInvite.id).all()
    return ok([reg_service.serialize_invite(i) for i in invites])


@admin_events_api.route(f"{P}/events/<int:event_id>/invites", methods=["POST"])
@event_permission_required(rbac.PERM_REGISTRATIONS_MANAGE)
def create_invite(event):
    data = json_body()
    invite, code = reg_service.create_invite(
        event,
        get_current_user(),
        label=v.string(data, "label", max_len=128),
        max_uses=v.integer(data, "max_uses", minimum=1, maximum=100000),
        expires_at=v.dt(data, "expires_at"),
    )
    audit("invite.create", event_id=event.id, target=("invite", invite.id))
    out = reg_service.serialize_invite(invite)
    out["code"] = code  # shown once; only the hash is stored
    return ok(out, status=201)


@admin_events_api.route(
    f"{P}/events/<int:event_id>/invites/<int:invite_id>", methods=["DELETE"]
)
@event_permission_required(rbac.PERM_REGISTRATIONS_MANAGE)
def revoke_invite(event, invite_id):
    invite = scoped_get_or_404(ArenaEventInvite, invite_id, event)
    reg_service.revoke_invite(invite)
    audit("invite.revoke", event_id=event.id, target=("invite", invite.id))
    return ok(reg_service.serialize_invite(invite))


# -------------------------------------------------------------------- export


@admin_events_api.route(f"{P}/events/<int:event_id>/export")
@event_permission_required(rbac.PERM_EVENT_MANAGE)
def export_event(event):
    regs = scoped_query(ArenaRegistration, event).order_by(ArenaRegistration.id).all()
    return ok(
        {
            "event": event_service.serialize_admin(event),
            "registrations": [reg_service.serialize(r) for r in regs],
        }
    )


# --------------------------------------------------------------------- roles


@admin_events_api.route(f"{P}/roles")
@global_permission_required(rbac.PERM_ROLES_MANAGE)
def list_roles():
    q = ArenaRole.query
    if request.args.get("event_id"):
        q = q.filter(
            ArenaRole.event_id == v.integer(request.args, "event_id", minimum=1)
        )
    return ok(
        [
            {"id": r.id, "user_id": r.user_id, "event_id": r.event_id, "role": r.role}
            for r in q.order_by(ArenaRole.id).all()
        ]
    )


@admin_events_api.route(f"{P}/roles", methods=["POST"])
@global_permission_required(rbac.PERM_ROLES_MANAGE)
def grant_role():
    data = json_body()
    user_id = v.integer(data, "user_id", required=True, minimum=1)
    role = v.choice(data, "role", ROLES, required=True)
    event_id = v.integer(data, "event_id", minimum=1)
    if Users.query.filter_by(id=user_id).first() is None:
        raise ApiError(404, "User not found")
    if event_id is not None and ArenaEvent.query.filter_by(id=event_id).first() is None:
        raise ApiError(404, "Event not found")
    actor = get_current_user()
    grant = rbac.grant_role(user_id, role, event_id=event_id, granted_by=actor.id)
    audit(
        "role.grant", event_id=event_id, target=("user", user_id), meta={"role": role}
    )
    return ok(
        {"id": grant.id, "user_id": user_id, "event_id": event_id, "role": role},
        status=201,
    )


@admin_events_api.route(f"{P}/roles/<int:role_id>", methods=["DELETE"])
@global_permission_required(rbac.PERM_ROLES_MANAGE)
def revoke_role(role_id):
    role = ArenaRole.query.filter_by(id=role_id).first()
    if role is None:
        raise ApiError(404)
    rbac.revoke_role(role.user_id, role.role, event_id=role.event_id)
    audit(
        "role.revoke",
        event_id=role.event_id,
        target=("user", role.user_id),
        meta={"role": role.role},
    )
    return ok({"deleted": role_id})


@admin_events_api.route(f"{P}/events/<int:event_id>/announcements", methods=["POST"])
@event_permission_required(rbac.PERM_EVENT_MANAGE)
def create_announcement(event):
    from CTFd.models import db
    from CTFd.plugins.arena.models import ArenaAnnouncement

    data = json_body()
    ann = ArenaAnnouncement(
        event_id=event.id,
        title=v.string(data, "title", required=True, max_len=128, allow_empty=False),
        body=v.string(data, "body", required=True, max_len=20000, allow_empty=False),
        pinned=bool(v.boolean(data, "pinned")),
        created_by=get_current_user().id,
    )
    db.session.add(ann)
    db.session.commit()
    audit("announcement.create", event_id=event.id, target=("announcement", ann.id))
    return ok({"id": ann.id}, status=201)


@admin_events_api.route(
    f"{P}/events/<int:event_id>/announcements/<int:ann_id>", methods=["DELETE"]
)
@event_permission_required(rbac.PERM_EVENT_MANAGE)
def delete_announcement(event, ann_id):
    from CTFd.models import db
    from CTFd.plugins.arena.models import ArenaAnnouncement

    ann = scoped_get_or_404(ArenaAnnouncement, ann_id, event)
    db.session.delete(ann)
    db.session.commit()
    audit("announcement.delete", event_id=event.id, target=("announcement", ann_id))
    return ok({"deleted": ann_id})

"""Organizer / challenge-manager API for event challenges."""

from flask import request

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
from CTFd.plugins.arena.core.authz import event_permission_required
from CTFd.plugins.arena.core.context import scoped_get_or_404, scoped_query
from CTFd.plugins.arena.models import (
    CATEGORIES,
    ArenaEvent,
    ArenaEventChallenge,
    ArenaSubmission,
)
from CTFd.plugins.arena.services import challenges as chal_service
from CTFd.plugins.arena.services import scoreboard
from CTFd.plugins.arena.services.audit import audit
from CTFd.plugins.challenges import CHALLENGE_CLASSES
from CTFd.utils.user import get_current_user

admin_challenges_api = api_blueprint("arena_admin_challenges_api", __name__)
A = f"{API_PREFIX}/admin/events/<int:event_id>"
VIEW = rbac.PERM_EVENT_VIEW_ADMIN
MANAGE = rbac.PERM_CHALLENGES_MANAGE


def _secrets_allowed(event):
    return rbac.has_permission(get_current_user(), MANAGE, event.id)


@admin_challenges_api.route(f"{A}/challenges/meta")
@event_permission_required(VIEW)
def meta(event):
    return ok({"types": sorted(CHALLENGE_CLASSES), "categories": list(CATEGORIES)})


@admin_challenges_api.route(f"{A}/challenges")
@event_permission_required(VIEW)
def list_challenges(event):
    rows = (
        scoped_query(ArenaEventChallenge, event)
        .order_by(ArenaEventChallenge.position, ArenaEventChallenge.id)
        .all()
    )
    return ok([chal_service.serialize_admin(ec) for ec in rows])


@admin_challenges_api.route(f"{A}/challenges", methods=["POST"])
@event_permission_required(MANAGE)
def create(event):
    ec = chal_service.create(event, json_body())
    audit("challenge.create", event_id=event.id, target=("challenge", ec.id))
    return ok(chal_service.serialize_admin(ec, include_secrets=True), status=201)


@admin_challenges_api.route(f"{A}/challenges/<int:ec_id>")
@event_permission_required(VIEW)
def get(event, ec_id):
    ec = scoped_get_or_404(ArenaEventChallenge, ec_id, event)
    # Flags are only shown to roles that can manage challenges (not analysts).
    return ok(chal_service.serialize_admin(ec, include_secrets=_secrets_allowed(event)))


@admin_challenges_api.route(f"{A}/challenges/<int:ec_id>", methods=["PATCH"])
@event_permission_required(MANAGE)
def update(event, ec_id):
    ec = scoped_get_or_404(ArenaEventChallenge, ec_id, event)
    data = json_body()
    chal_service.update(event, ec, data)
    scoreboard.invalidate(event.id)  # points/scoring changes affect standings
    audit(
        "challenge.update",
        event_id=event.id,
        target=("challenge", ec.id),
        meta={"fields": sorted(data)},
    )
    return ok(chal_service.serialize_admin(ec, include_secrets=True))


@admin_challenges_api.route(f"{A}/challenges/<int:ec_id>", methods=["DELETE"])
@event_permission_required(MANAGE)
def delete(event, ec_id):
    ec = scoped_get_or_404(ArenaEventChallenge, ec_id, event)
    chal_service.delete(event, ec)
    audit("challenge.delete", event_id=event.id, target=("challenge", ec_id))
    return ok({"deleted": ec_id})


@admin_challenges_api.route(f"{A}/challenges/<int:ec_id>/clone", methods=["POST"])
@event_permission_required(MANAGE)
def clone(event, ec_id):
    ec = scoped_get_or_404(ArenaEventChallenge, ec_id, event)
    target_id = v.integer(json_body(), "target_event_id", required=True, minimum=1)
    target = ArenaEvent.query.filter_by(id=target_id).first()
    # Must also be allowed to manage challenges of the TARGET event.
    if target is None or not rbac.has_permission(get_current_user(), MANAGE, target.id):
        raise ApiError(404, "Target event not found")
    new = chal_service.clone(event, ec, target)
    audit(
        "challenge.clone",
        event_id=target.id,
        target=("challenge", new.id),
        meta={"from": ec.id},
    )
    return ok(chal_service.serialize_admin(new), status=201)


@admin_challenges_api.route(f"{A}/challenges/<int:ec_id>/flags", methods=["POST"])
@event_permission_required(MANAGE)
def add_flag(event, ec_id):
    ec = scoped_get_or_404(ArenaEventChallenge, ec_id, event)
    flag = chal_service.add_flag(ec, json_body())
    audit("challenge.flag_add", event_id=event.id, target=("challenge", ec.id))
    return ok({"id": flag.id, "type": flag.type}, status=201)


@admin_challenges_api.route(
    f"{A}/challenges/<int:ec_id>/flags/<int:flag_id>", methods=["DELETE"]
)
@event_permission_required(MANAGE)
def delete_flag(event, ec_id, flag_id):
    ec = scoped_get_or_404(ArenaEventChallenge, ec_id, event)
    chal_service.delete_flag(ec, flag_id)
    audit("challenge.flag_delete", event_id=event.id, target=("challenge", ec.id))
    return ok({"deleted": flag_id})


@admin_challenges_api.route(f"{A}/challenges/<int:ec_id>/hints", methods=["POST"])
@event_permission_required(MANAGE)
def add_hint(event, ec_id):
    ec = scoped_get_or_404(ArenaEventChallenge, ec_id, event)
    hint = chal_service.add_hint(ec, json_body())
    return ok({"id": hint.id, "title": hint.title, "cost": hint.cost}, status=201)


@admin_challenges_api.route(
    f"{A}/challenges/<int:ec_id>/hints/<int:hint_id>", methods=["PATCH"]
)
@event_permission_required(MANAGE)
def update_hint(event, ec_id, hint_id):
    ec = scoped_get_or_404(ArenaEventChallenge, ec_id, event)
    hint = chal_service.update_hint(ec, hint_id, json_body())
    return ok({"id": hint.id, "title": hint.title, "cost": hint.cost})


@admin_challenges_api.route(
    f"{A}/challenges/<int:ec_id>/hints/<int:hint_id>", methods=["DELETE"]
)
@event_permission_required(MANAGE)
def delete_hint(event, ec_id, hint_id):
    ec = scoped_get_or_404(ArenaEventChallenge, ec_id, event)
    chal_service.delete_hint(ec, hint_id)
    return ok({"deleted": hint_id})


@admin_challenges_api.route(f"{A}/challenges/<int:ec_id>/files", methods=["POST"])
@event_permission_required(MANAGE)
def add_file(event, ec_id):
    ec = scoped_get_or_404(ArenaEventChallenge, ec_id, event)
    f = chal_service.add_file(ec, request.files.get("file"))
    audit("challenge.file_add", event_id=event.id, target=("challenge", ec.id))
    return ok({"id": f.id, "name": f.location.rsplit("/", 1)[-1]}, status=201)


@admin_challenges_api.route(
    f"{A}/challenges/<int:ec_id>/files/<int:file_id>", methods=["DELETE"]
)
@event_permission_required(MANAGE)
def delete_file(event, ec_id, file_id):
    ec = scoped_get_or_404(ArenaEventChallenge, ec_id, event)
    chal_service.delete_file(ec, file_id)
    return ok({"deleted": file_id})


@admin_challenges_api.route(f"{A}/submissions")
@event_permission_required(VIEW)
def submissions(event):
    q = scoped_query(ArenaSubmission, event)
    for f in ("status", "entity_key"):
        if request.args.get(f):
            q = q.filter(getattr(ArenaSubmission, f) == request.args[f][:32])
    if request.args.get("challenge_id"):
        q = q.filter(
            ArenaSubmission.event_challenge_id
            == v.integer(request.args, "challenge_id")
        )
    page, per_page = pagination(default_per_page=50)
    show_preview = _secrets_allowed(event)
    data, meta = paginate(
        q.order_by(ArenaSubmission.id.desc()),
        page,
        per_page,
        lambda s: {
            "id": s.id,
            "challenge_id": s.event_challenge_id,
            "entity_key": s.entity_key,
            "user_id": s.user_id,
            "status": s.status,
            "date": v.iso(s.created_at),
            "provided": s.provided_preview if show_preview else None,
        },
    )
    return ok(data, meta=meta)

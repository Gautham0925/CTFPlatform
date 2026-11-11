"""Challenge instance API (participants) and image/runner admin API."""

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
    event_participant_required,
    event_permission_required,
    global_permission_required,
)
from CTFd.plugins.arena.core.context import scoped_get_or_404, scoped_query
from CTFd.plugins.arena.core.ratelimit import ratelimit
from CTFd.plugins.arena.execution.client import RunnerError, get_client
from CTFd.plugins.arena.models import ArenaEventChallenge, ArenaImage, ArenaInstance
from CTFd.plugins.arena.services import activity
from CTFd.plugins.arena.services import entity as entity_service
from CTFd.plugins.arena.services import events as event_service
from CTFd.plugins.arena.services import instances as inst_service
from CTFd.plugins.arena.services import submissions as sub_service
from CTFd.plugins.arena.services.audit import audit
from CTFd.utils.user import get_current_user

instances_api = api_blueprint("arena_instances_api", __name__)
E = f"{API_PREFIX}/events/<int:event_id>"
A = f"{API_PREFIX}/admin/events/<int:event_id>"
P = f"{API_PREFIX}/admin/platform"


def _ctx(event, ec_id, require_open=True):
    user = get_current_user()
    entity = entity_service.resolve(event, user)
    ec = scoped_get_or_404(ArenaEventChallenge, ec_id, event)
    state = sub_service.challenge_state(
        event, ec, sub_service.solved_ids(event, entity.key)
    )
    if state == "hidden":
        raise ApiError(404)
    if state == "locked":
        raise ApiError(403, "Challenge is locked", code="locked")
    if require_open and (state != "open" or not event_service.is_running(event)):
        raise ApiError(
            403,
            "Instances are only available while the challenge is open",
            code="closed",
        )
    return user, entity, ec


@instances_api.route(f"{E}/challenges/<int:ec_id>/instance", methods=["POST"])
@ratelimit(method="POST", limit=10, interval=60, key_prefix="arena-instance")
@event_participant_required
def request_instance(event, ec_id):
    user, entity, ec = _ctx(event, ec_id)
    inst, created = inst_service.request_instance(event, ec, entity, user)
    if created:
        activity.record(
            "instance.allocate",
            user_id=user.id,
            event_id=event.id,
            target=("instance", inst.id),
            meta={"challenge": ec.id},
        )
    return ok(inst_service.serialize(inst), status=202 if created else 200)


@instances_api.route(f"{E}/challenges/<int:ec_id>/instance")
@event_participant_required
def get_instance(event, ec_id):
    _user, entity, ec = _ctx(event, ec_id, require_open=False)
    inst = inst_service.current(event, ec, entity.key)
    return ok(inst_service.serialize(inst) if inst else None)


@instances_api.route(f"{E}/challenges/<int:ec_id>/instance", methods=["DELETE"])
@event_participant_required
def release_instance(event, ec_id):
    user, entity, ec = _ctx(event, ec_id, require_open=False)
    inst = inst_service.current(event, ec, entity.key)
    if inst is None:
        raise ApiError(404, "No active instance")
    inst_service.release(inst)
    activity.record(
        "instance.release",
        user_id=user.id,
        event_id=event.id,
        target=("instance", inst.id),
    )
    return ok(inst_service.serialize(inst))


@instances_api.route(f"{E}/challenges/<int:ec_id>/instance/restart", methods=["POST"])
@ratelimit(method="POST", limit=5, interval=60, key_prefix="arena-instance-restart")
@event_participant_required
def restart_instance(event, ec_id):
    _user, entity, ec = _ctx(event, ec_id)
    inst = inst_service.current(event, ec, entity.key)
    if inst is None:
        raise ApiError(404, "No active instance")
    inst_service.restart(inst)
    return ok(inst_service.serialize(inst), status=202)


@instances_api.route(f"{E}/my-instances")
@event_participant_required
def my_instances(event):
    entity = entity_service.resolve(event, get_current_user(), require=False)
    if entity is None:
        return ok([])
    rows = (
        scoped_query(ArenaInstance, event)
        .filter(
            ArenaInstance.entity_key == entity.key, ArenaInstance.active_key.isnot(None)
        )
        .all()
    )
    return ok([inst_service.serialize(i) for i in rows])


# ------------------------------------------------------------------- admin


@instances_api.route(f"{A}/instances")
@event_permission_required(rbac.PERM_EVENT_VIEW_ADMIN)
def admin_instances(event):
    q = scoped_query(ArenaInstance, event)
    from flask import request

    if request.args.get("status"):
        q = q.filter(ArenaInstance.status == request.args["status"][:16])
    page, per_page = pagination(default_per_page=50)
    data, meta = paginate(
        q.order_by(ArenaInstance.id.desc()),
        page,
        per_page,
        lambda i: inst_service.serialize(i, include_internal=True),
    )
    meta["counts"] = inst_service.counts_by_status(event)
    return ok(data, meta=meta)


@instances_api.route(f"{A}/instances/<int:inst_id>/stop", methods=["POST"])
@event_permission_required(rbac.PERM_LABS_MANAGE)
def admin_stop(event, inst_id):
    inst = scoped_get_or_404(ArenaInstance, inst_id, event)
    inst_service.release(inst, reason="stopped by organizer")
    audit("instance.stop", event_id=event.id, target=("instance", inst.id))
    return ok(inst_service.serialize(inst, include_internal=True))


@instances_api.route(f"{P}/images")
@global_permission_required(rbac.PERM_PLATFORM_MANAGE)
def list_images():
    return ok(
        [
            inst_service.serialize_image(i)
            for i in ArenaImage.query.order_by(ArenaImage.name)
        ]
    )


@instances_api.route(f"{P}/images", methods=["POST"])
@global_permission_required(rbac.PERM_PLATFORM_MANAGE)
def create_image():
    img = inst_service.create_image(json_body())
    audit("image.register", target=("image", img.id), meta={"reference": img.reference})
    return ok(inst_service.serialize_image(img), status=201)


@instances_api.route(f"{P}/images/<int:image_id>", methods=["PATCH"])
@global_permission_required(rbac.PERM_PLATFORM_MANAGE)
def update_image(image_id):
    from CTFd.models import db

    img = db.session.get(ArenaImage, image_id)
    if img is None:
        raise ApiError(404)
    data = inst_service.validate_image_data(json_body(), creating=False)
    if "reference" in data and data["reference"] != img.reference:
        img.validated_at = None  # new digest must be validated again
    for k, val in data.items():
        setattr(img, k, val)
    db.session.commit()
    audit("image.update", target=("image", img.id), meta={"fields": sorted(data)})
    return ok(inst_service.serialize_image(img))


@instances_api.route(f"{P}/images/<int:image_id>/validate", methods=["POST"])
@global_permission_required(rbac.PERM_PLATFORM_MANAGE)
def validate_image(image_id):
    from CTFd.models import db

    img = db.session.get(ArenaImage, image_id)
    if img is None:
        raise ApiError(404)
    inst_service.validate_with_runner(img)
    audit("image.validate", target=("image", img.id))
    return ok(inst_service.serialize_image(img))


@instances_api.route(f"{P}/runner/health")
@global_permission_required(rbac.PERM_PLATFORM_MANAGE)
def runner_health():
    client = get_client()
    try:
        return ok({"mode": client.mode, "health": client.health()})
    except RunnerError as e:
        return ok({"mode": client.mode, "health": None, "error": str(e)})

"""External lab API."""

from CTFd.models import db
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
from CTFd.plugins.arena.labs import make_provider
from CTFd.plugins.arena.labs.base import ProviderError
from CTFd.plugins.arena.models import ArenaEventLab, ArenaLabProvider, ArenaLabSession
from CTFd.plugins.arena.services import activity
from CTFd.plugins.arena.services import entity as entity_service
from CTFd.plugins.arena.services import events as event_service
from CTFd.plugins.arena.services import labs as lab_service
from CTFd.plugins.arena.services.audit import audit
from CTFd.utils.user import get_current_user

labs_api = api_blueprint("arena_labs_api", __name__)
P = f"{API_PREFIX}/admin/platform/lab-providers"
A = f"{API_PREFIX}/admin/events/<int:event_id>/labs"
E = f"{API_PREFIX}/events/<int:event_id>/labs"
PLATFORM = rbac.PERM_PLATFORM_MANAGE


def _provider(pid):
    row = db.session.get(ArenaLabProvider, pid)
    if row is None:
        raise ApiError(404)
    return row


# ------------------------------------------------------------- providers


@labs_api.route(P)
@global_permission_required(PLATFORM)
def list_providers():
    return ok(
        [
            lab_service.serialize_provider(p)
            for p in ArenaLabProvider.query.order_by(ArenaLabProvider.name)
        ]
    )


@labs_api.route(P, methods=["POST"])
@global_permission_required(PLATFORM)
def create_provider():
    row = lab_service.save_provider(json_body())
    audit(
        "lab_provider.create",
        target=("lab_provider", row.id),
        meta={"adapter": row.adapter},
    )
    return ok(lab_service.serialize_provider(row), status=201)


@labs_api.route(f"{P}/<int:pid>", methods=["PATCH"])
@global_permission_required(PLATFORM)
def update_provider(pid):
    data = json_body()
    data.pop("adapter", None)
    row = lab_service.save_provider(data, row=_provider(pid))
    audit(
        "lab_provider.update",
        target=("lab_provider", row.id),
        meta={"fields": sorted(data)},
    )
    return ok(lab_service.serialize_provider(row))


@labs_api.route(f"{P}/<int:pid>/test", methods=["POST"])
@ratelimit(method="POST", limit=10, interval=60, key_prefix="arena-labp-test")
@global_permission_required(PLATFORM)
def test_provider(pid):
    row = _provider(pid)
    health = lab_service.check_health(row)
    try:
        labs = make_provider(row).list_labs()
    except ProviderError as e:
        labs, health = [], dict(health, labs_error=str(e)[:200])
    return ok({"health": health, "labs": labs})


# ----------------------------------------------------------- event labs


@labs_api.route(A)
@event_permission_required(rbac.PERM_EVENT_VIEW_ADMIN)
def admin_list(event):
    return ok(
        [
            lab_service.serialize_event_lab(lab)
            for lab in scoped_query(ArenaEventLab, event)
        ]
    )


@labs_api.route(A, methods=["POST"])
@event_permission_required(rbac.PERM_LABS_MANAGE)
def attach(event):
    lab = lab_service.create_event_lab(event, json_body())
    audit("lab.attach", event_id=event.id, target=("event_lab", lab.id))
    return ok(lab_service.serialize_event_lab(lab), status=201)


@labs_api.route(f"{A}/<int:lab_id>", methods=["PATCH"])
@event_permission_required(rbac.PERM_LABS_MANAGE)
def update_lab(event, lab_id):
    from CTFd.plugins.arena import validation as v

    lab = scoped_get_or_404(ArenaEventLab, lab_id, event)
    data = json_body()
    if "enabled" in data:
        lab.enabled = bool(v.boolean(data, "enabled"))
    for f, lo, hi in (
        ("ttl_minutes", 5, 1440),
        ("max_sessions", 1, 10000),
        ("completion_points", 0, 100000),
    ):
        if f in data:
            setattr(lab, f, v.integer(data, f, minimum=lo, maximum=hi, required=True))
    db.session.commit()
    audit(
        "lab.update",
        event_id=event.id,
        target=("event_lab", lab.id),
        meta={"fields": sorted(data)},
    )
    return ok(lab_service.serialize_event_lab(lab))


@labs_api.route(f"{A}/sessions")
@event_permission_required(rbac.PERM_EVENT_VIEW_ADMIN)
def admin_sessions(event):
    page, per_page = pagination(default_per_page=50)
    data, meta = paginate(
        scoped_query(ArenaLabSession, event).order_by(ArenaLabSession.id.desc()),
        page,
        per_page,
        lambda s: lab_service.serialize_session(s, admin=True),
    )
    return ok(data, meta=meta)


@labs_api.route(f"{A}/sessions/<int:sid>/release", methods=["POST"])
@event_permission_required(rbac.PERM_LABS_MANAGE)
def admin_release(event, sid):
    sess = scoped_get_or_404(ArenaLabSession, sid, event)
    lab_service.release(sess, reason="released by organizer")
    audit("lab.session_release", event_id=event.id, target=("lab_session", sess.id))
    return ok(lab_service.serialize_session(sess, admin=True))


# ---------------------------------------------------------- participants


def _ctx(event, lab_id):
    lab = scoped_get_or_404(ArenaEventLab, lab_id, event)
    if not lab.enabled:
        raise ApiError(404)
    return lab, entity_service.resolve(event, get_current_user())


@labs_api.route(E)
@event_participant_required
def my_labs(event):
    ent = entity_service.resolve(event, get_current_user(), require=False)
    out = []
    for lab in scoped_query(ArenaEventLab, event).filter_by(enabled=True):
        sess = lab_service.current(lab, ent.key) if ent else None
        out.append(
            {
                "id": lab.id,
                "name": lab.name,
                "description": lab.description,
                "ttl_minutes": lab.ttl_minutes,
                "completion_points": lab.completion_points,
                "session": lab_service.serialize_session(sess, owner=True)
                if sess
                else None,
            }
        )
    return ok(out)


@labs_api.route(f"{E}/<int:lab_id>/session", methods=["POST"])
@ratelimit(method="POST", limit=10, interval=60, key_prefix="arena-lab-alloc")
@event_participant_required
def allocate(event, lab_id):
    lab, ent = _ctx(event, lab_id)
    if not event_service.is_running(event):
        raise ApiError(
            403,
            "Labs are only available while the event runs",
            code="event_not_running",
        )
    sess, created = lab_service.request_session(event, lab, ent, get_current_user())
    if created:
        activity.record(
            "lab.allocate",
            user_id=get_current_user().id,
            event_id=event.id,
            target=("lab", lab.id),
        )
    return ok(
        lab_service.serialize_session(sess, owner=True), status=202 if created else 200
    )


@labs_api.route(f"{E}/<int:lab_id>/session")
@event_participant_required
def get_session(event, lab_id):
    lab, ent = _ctx(event, lab_id)
    sess = lab_service.current(lab, ent.key)
    if sess is not None and sess.status == "ready":
        activity.record(
            "lab.access",
            user_id=get_current_user().id,
            event_id=event.id,
            target=("lab", lab.id),
        )
    return ok(lab_service.serialize_session(sess, owner=True) if sess else None)


@labs_api.route(f"{E}/<int:lab_id>/session/reset", methods=["POST"])
@ratelimit(method="POST", limit=5, interval=60, key_prefix="arena-lab-reset")
@event_participant_required
def reset(event, lab_id):
    lab, ent = _ctx(event, lab_id)
    sess = lab_service.current(lab, ent.key)
    if sess is None:
        raise ApiError(404, "No active lab session")
    lab_service.reset(sess)
    return ok(lab_service.serialize_session(sess, owner=True), status=202)


@labs_api.route(f"{E}/<int:lab_id>/session", methods=["DELETE"])
@event_participant_required
def release(event, lab_id):
    lab, ent = _ctx(event, lab_id)
    sess = lab_service.current(lab, ent.key)
    if sess is None:
        raise ApiError(404, "No active lab session")
    lab_service.release(sess)
    activity.record(
        "lab.release",
        user_id=get_current_user().id,
        event_id=event.id,
        target=("lab", lab.id),
    )
    return ok(lab_service.serialize_session(sess))

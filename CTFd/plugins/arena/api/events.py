"""Participant-facing event & registration API (/api/v1/arena)."""

from flask import abort

from CTFd.plugins.arena.api import (
    API_PREFIX,
    ApiError,
    api_blueprint,
    json_body,
    ok,
    paginate,
    pagination,
)
from CTFd.plugins.arena.core.authz import event_view
from CTFd.plugins.arena.core.context import (
    get_registration,
    listed_events_query,
    resolve_event,
)
from CTFd.plugins.arena.core.ratelimit import ratelimit
from CTFd.plugins.arena.models import ArenaEvent, ArenaRegistration
from CTFd.plugins.arena.services import activity
from CTFd.plugins.arena.services import events as event_service
from CTFd.plugins.arena.services import registration as reg_service
from CTFd.utils.user import authed, get_current_user

events_api = api_blueprint("arena_events_api", __name__)


def _require_user():
    if not authed():
        raise ApiError(401)
    return get_current_user()


@events_api.route(f"{API_PREFIX}/events")
def list_events():
    user = get_current_user() if authed() else None
    page, per_page = pagination()
    q = listed_events_query(user)
    phase = None
    from flask import request

    phase = request.args.get("phase")
    q = q.order_by(
        ArenaEvent.starts_at.is_(None), ArenaEvent.starts_at.asc(), ArenaEvent.id
    )
    regs = {}
    if user is not None:
        regs = {
            r.event_id: r
            for r in ArenaRegistration.query.filter_by(user_id=user.id).all()
        }
    data, meta = paginate(
        q, page, per_page, lambda e: event_service.serialize_public(e, regs.get(e.id))
    )
    if phase:
        data = [d for d in data if d["phase"] == phase]
    return ok(data, meta=meta)


@events_api.route(f"{API_PREFIX}/events/<int:event_id>")
@event_view
def get_event(event):
    user = get_current_user() if authed() else None
    return ok(event_service.serialize_public(event, get_registration(event, user)))


@events_api.route(f"{API_PREFIX}/events/by-slug/<slug>")
def get_event_by_slug(slug):
    event = ArenaEvent.query.filter_by(slug=str(slug)[:64].lower()).first()
    if event is None:
        abort(404)
    event = resolve_event(event.id)  # applies visibility rules (404 if hidden)
    user = get_current_user() if authed() else None
    return ok(event_service.serialize_public(event, get_registration(event, user)))


@events_api.route(f"{API_PREFIX}/events/<int:event_id>/register", methods=["POST"])
@ratelimit(method="POST", limit=10, interval=60, key_prefix="arena-register")
@event_view
def register(event):
    user = _require_user()
    data = json_body()
    reg = reg_service.register(event, user, invite_code=data.get("invite_code"))
    activity.record(
        "registration.register",
        user_id=user.id,
        event_id=event.id,
        target=("registration", reg.id),
        meta={"status": reg.status},
    )
    return ok(reg_service.serialize(reg), status=201)


@events_api.route(f"{API_PREFIX}/events/join", methods=["POST"])
@ratelimit(method="POST", limit=10, interval=60, key_prefix="arena-join")
def join_with_code():
    """Register with an invitation code (the only way into private events)."""
    user = _require_user()
    code = json_body().get("invite_code")
    invite = reg_service.find_invite_by_code(code)
    if invite is None:
        # Same response for unknown and unusable codes: no enumeration.
        raise ApiError(
            403, "A valid invitation code is required", code="invite_required"
        )
    event = ArenaEvent.query.filter_by(id=invite.event_id).first()
    if event is None or event.status not in ("upcoming", "active"):
        raise ApiError(
            403, "A valid invitation code is required", code="invite_required"
        )
    reg = reg_service.register(event, user, invite_code=code)
    activity.record(
        "registration.join_code",
        user_id=user.id,
        event_id=event.id,
        target=("registration", reg.id),
    )
    return ok(
        {
            "registration": reg_service.serialize(reg),
            "event": {"id": event.id, "slug": event.slug},
        },
        status=201,
    )


@events_api.route(f"{API_PREFIX}/events/<int:event_id>/withdraw", methods=["POST"])
@event_view
def withdraw(event):
    user = _require_user()
    reg = reg_service.withdraw(event, user)
    activity.record(
        "registration.withdraw",
        user_id=user.id,
        event_id=event.id,
        target=("registration", reg.id),
    )
    return ok(reg_service.serialize(reg))


@events_api.route(f"{API_PREFIX}/me/events")
def my_events():
    user = _require_user()
    regs = ArenaRegistration.query.filter_by(user_id=user.id).all()
    out = []
    for r in regs:
        event = ArenaEvent.query.filter_by(id=r.event_id).first()
        if event is None:
            continue
        # A user always sees their own registrations, but hidden (draft)
        # events are only shown by id/status, without details.
        from CTFd.plugins.arena.core.context import can_view_event

        if can_view_event(event, user):
            out.append(event_service.serialize_public(event, r))
    return ok(out)


# ------------------------------------------------------------ announcements


@events_api.route(f"{API_PREFIX}/events/<int:event_id>/announcements")
@event_view
def announcements(event):
    from CTFd.plugins.arena.models import ArenaAnnouncement
    from CTFd.plugins.arena.validation import iso

    rows = (
        ArenaAnnouncement.query.filter_by(event_id=event.id)
        .order_by(ArenaAnnouncement.pinned.desc(), ArenaAnnouncement.id.desc())
        .limit(50)
        .all()
    )
    return ok(
        [
            {
                "id": a.id,
                "title": a.title,
                "html": event_service.render_markdown(a.body),
                "pinned": a.pinned,
                "created_at": iso(a.created_at),
            }
            for a in rows
        ]
    )

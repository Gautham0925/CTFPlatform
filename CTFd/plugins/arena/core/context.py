"""Event context resolution.

Rules (see docs/SECURITY_PLAN.md):
* The event is resolved ONLY from the URL route parameter. Event ids found in
  query strings, forms or JSON bodies are never used for authorization.
* An event that the caller may not see returns 404 (not 403) so existence
  cannot be probed.
* Child objects must be loaded with ``scoped_query``/``scoped_get_or_404``,
  which always constrain ``event_id`` to the resolved event.
"""

from flask import abort, g

from CTFd.plugins.arena.core import rbac
from CTFd.plugins.arena.models import ArenaEvent, ArenaRegistration
from CTFd.utils.user import get_current_user

# Statuses that non-privileged users may ever see.
PUBLIC_STATUSES = frozenset({"published", "running", "paused", "ended", "archived"})
# Registration statuses that let a user see a private event.
PRIVATE_VIEW_REGISTRATION = frozenset({"pending", "approved"})


def get_registration(event, user):
    if user is None:
        return None
    return ArenaRegistration.query.filter_by(event_id=event.id, user_id=user.id).first()


def can_view_event(event, user):
    if rbac.has_any_role(user, event.id):
        return True
    if event.status not in PUBLIC_STATUSES:
        return False
    if event.visibility in ("public", "unlisted"):
        return True
    # private
    reg = get_registration(event, user)
    return reg is not None and reg.status in PRIVATE_VIEW_REGISTRATION


def is_listed_for(event, user):
    """Whether the event appears in event listings (unlisted = link only)."""
    if rbac.has_any_role(user, event.id):
        return True
    return event.visibility != "unlisted" and can_view_event(event, user)


def resolve_event(event_id, user=None):
    """Load an event the current user may view, else 404."""
    try:
        event_id = int(event_id)
    except (TypeError, ValueError):
        abort(404)
    if user is None:
        user = get_current_user()
    event = ArenaEvent.query.filter_by(id=event_id).first()
    if event is None or not can_view_event(event, user):
        abort(404)
    g.arena_event = event
    return event


def current_event():
    event = getattr(g, "arena_event", None)
    if event is None:
        raise RuntimeError("No Arena event resolved for this request")
    return event


def scoped_query(model, event):
    """Query ``model`` restricted to ``event``. The model must have event_id."""
    if not hasattr(model, "event_id"):
        raise TypeError(f"{model.__name__} is not event-scoped")
    return model.query.filter(model.event_id == event.id)


def scoped_get_or_404(model, object_id, event):
    obj = scoped_query(model, event).filter(model.id == object_id).first()
    if obj is None:
        abort(404)
    return obj

"""Route guards for event-scoped views.

Usage::

    @bp.route("/api/v1/arena/events/<int:event_id>/thing")
    @event_participant_required
    def view(event):            # event_id is replaced by the resolved event
        ...

The decorators take the event id ONLY from the URL route (``view_args``).
A route without ``event_id`` is a programming error and fails closed (500)
instead of being silently unguarded.
"""

import functools

from flask import abort, g, request

from CTFd.plugins.arena.core import rbac
from CTFd.plugins.arena.core.context import get_registration, resolve_event
from CTFd.utils import get_config
from CTFd.utils.user import authed, get_current_user


class GuardMisconfigured(RuntimeError):
    pass


def _pop_event(kwargs):
    if "event_id" not in kwargs or request.view_args is None:
        raise GuardMisconfigured("Arena event guard used on a route without <event_id>")
    # Paranoia: the value must be the one the URL router extracted.
    if kwargs["event_id"] != request.view_args.get("event_id"):
        raise GuardMisconfigured("event_id does not come from the URL route")
    return kwargs.pop("event_id")


def _deny_unauthenticated():
    # 401 for API callers; matches native semantics of "not logged in".
    abort(401)


def event_view(f):
    """Resolve a viewable event (anonymous allowed for public events)."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        event_id = _pop_event(kwargs)
        event = resolve_event(event_id)
        return f(*args, event=event, **kwargs)

    return wrapper


def event_participant_required(f):
    """Caller must be logged in with an APPROVED registration for the event."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        event_id = _pop_event(kwargs)
        if not authed():
            _deny_unauthenticated()
        user = get_current_user()
        event = resolve_event(event_id, user=user)
        if user.banned:
            abort(403)
        if get_config("verify_emails") and not user.verified:
            abort(403)
        reg = get_registration(event, user)
        if reg is None or reg.status != "approved":
            abort(403)
        g.arena_registration = reg
        return f(*args, event=event, **kwargs)

    return wrapper


def event_permission_required(permission):
    """Caller must hold ``permission`` for the event (superadmins always do)."""
    if permission not in rbac.ALL_PERMISSIONS:
        raise rbac.UnknownPermission(permission)

    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            event_id = _pop_event(kwargs)
            if not authed():
                _deny_unauthenticated()
            user = get_current_user()
            # Non-role holders cannot see draft events -> 404 here, which is
            # the desired non-disclosure behaviour.
            event = resolve_event(event_id, user=user)
            if not rbac.has_permission(user, permission, event_id=event.id):
                abort(403)
            return f(*args, event=event, **kwargs)

        return wrapper

    return decorator


def global_permission_required(permission):
    """For non-event routes, e.g. creating events (superadmin-only perms)."""
    if permission not in rbac.ALL_PERMISSIONS:
        raise rbac.UnknownPermission(permission)

    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            if not authed():
                _deny_unauthenticated()
            user = get_current_user()
            if not rbac.has_permission(user, permission, event_id=None):
                abort(403)
            return f(*args, **kwargs)

        return wrapper

    return decorator

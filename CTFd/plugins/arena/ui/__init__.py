"""Server-rendered page shells for the participant and organizer dashboards.

Each view enforces the same visibility/authorization rules as the JSON API
before rendering; the pages then load data from the (already guarded) API.
Hidden UI elements are never relied on for access control.
"""

from flask import Blueprint, abort, redirect, render_template, request, url_for

from CTFd.plugins.arena.core import rbac
from CTFd.plugins.arena.core.context import get_registration, resolve_event
from CTFd.plugins.arena.models import ArenaEvent, ArenaRole
from CTFd.utils.user import authed, get_current_user

ui = Blueprint("arena_ui", __name__)
T = "plugins/arena/templates/arena/"
EVENT_PAGES = {
    "overview": "Overview",
    "challenges": "Challenges",
    "scoreboard": "Scoreboard",
    "team": "Team",
    "ad": "Attack & Defense",
    "labs": "Labs",
    "me": "My progress",
}


def _render(page, title, **ctx):
    from flask import current_app

    settings = current_app.extensions["arena"]["settings"]
    from CTFd.plugins.arena import ARENA_VERSION

    return render_template(
        T + "page.html",
        page=page,
        title=title,
        arena_version=ARENA_VERSION,
        certificates_enabled=settings.certificates_enabled,
        **ctx,
    )


def _event_by_slug(slug):
    event = ArenaEvent.query.filter_by(slug=str(slug)[:64].lower()).first()
    if event is None:
        abort(404)
    return resolve_event(event.id)  # 404 when not visible to this user


@ui.route("/arena")
def home():
    return _render("events", "Competitions")


@ui.route("/arena/join")
def join():
    if not authed():
        return redirect(url_for("auth.login", next=request.full_path))
    return _render("join", "Join with an invitation code")


@ui.route("/arena/e/<slug>")
@ui.route("/arena/e/<slug>/<section>")
def event_page(slug, section="overview"):
    if section not in EVENT_PAGES:
        abort(404)
    event = _event_by_slug(slug)
    if section == "ad" and not event.ad_enabled:
        abort(404)
    if section == "challenges" and not event.jeopardy_enabled:
        abort(404)
    if section not in ("overview", "scoreboard") and not authed():
        return redirect(url_for("auth.login", next=request.full_path))
    user = get_current_user() if authed() else None
    reg = get_registration(event, user)
    tabs = [
        (k, v)
        for k, v in EVENT_PAGES.items()
        if not (k == "ad" and not event.ad_enabled)
        and not (k == "challenges" and not event.jeopardy_enabled)
        and not (k == "team" and event.team_mode != "team")
    ]
    return _render(
        f"event-{section}",
        f"{event.name} · {EVENT_PAGES[section]}",
        event=event,
        tabs=tabs,
        section=section,
        registration=reg.status if reg else None,
        can_admin=user is not None and rbac.has_any_role(user, event.id),
    )


@ui.route("/arena/verify/<public_id>")
def verify(public_id):
    from flask import current_app

    if not current_app.extensions["arena"]["settings"].certificates_enabled:
        abort(404)
    return _render(
        "verify",
        "Certificate verification",
        public_id=public_id[:36],
        code=request.args.get("code", "")[:64],
    )


def _require_any_admin_role():
    if not authed():
        abort(401)
    user = get_current_user()
    if rbac.is_superadmin(user):
        return user
    if ArenaRole.query.filter_by(user_id=user.id).first() is None:
        abort(403)
    return user


@ui.route("/arena/admin")
def admin_home():
    user = _require_any_admin_role()
    return _render(
        "admin-home", "Arena administration", is_superadmin=rbac.is_superadmin(user)
    )


@ui.route("/arena/admin/platform")
def admin_platform():
    user = _require_any_admin_role()
    if not rbac.is_superadmin(user):
        abort(403)
    return _render("admin-platform", "Platform operations", is_superadmin=True)


@ui.route("/arena/admin/e/<int:event_id>")
def admin_event(event_id):
    user = _require_any_admin_role()
    event = resolve_event(event_id, user=user)
    if not rbac.has_permission(user, rbac.PERM_EVENT_VIEW_ADMIN, event.id):
        abort(403)
    perms = sorted(
        p
        for p in rbac.ALL_PERMISSIONS - rbac.SUPERADMIN_ONLY
        if rbac.has_permission(user, p, event.id)
    )
    return _render(
        "admin-event",
        f"Manage · {event.name}",
        event=event,
        perms=perms,
        is_superadmin=rbac.is_superadmin(user),
    )

"""Authorization / IDOR matrix for Arena event guards.

Every guard type is exercised by every actor class against four events
(public, private, draft, unlisted). Expected results come from an independent
oracle (``expected``) that encodes docs/SECURITY_PLAN.md, not from the code
under test.
"""

import pytest
from flask import Blueprint, g, jsonify, request

from CTFd.cache import clear_user_session
from CTFd.models import Users, db
from CTFd.plugins.arena.core import authz
from CTFd.plugins.arena.core.authz import (
    GuardMisconfigured,
    event_participant_required,
    event_permission_required,
    event_view,
    global_permission_required,
)
from CTFd.plugins.arena.core.context import scoped_get_or_404
from CTFd.plugins.arena.models import ArenaRegistration
from tests.arena.helpers import (
    create_arena_ctfd,
    gen_event,
    gen_registration,
    gen_role,
    make_user,
)
from tests.helpers import destroy_ctfd, login_as_user

bp = Blueprint("arena_test_guards", __name__)


def _echo(event):
    return jsonify({"event": event.id, "body": request.get_json(silent=True)})


@bp.route("/t/e/<int:event_id>/view", methods=["GET", "POST"])
@event_view
def t_view(event):
    return _echo(event)


@bp.route("/t/e/<int:event_id>/part", methods=["GET", "POST"])
@event_participant_required
def t_part(event):
    return _echo(event)


@bp.route("/t/e/<int:event_id>/manage", methods=["GET", "POST"])
@event_permission_required("event.manage")
def t_manage(event):
    return _echo(event)


@bp.route("/t/e/<int:event_id>/audit")
@event_permission_required("audit.read")
def t_audit(event):
    return _echo(event)


@bp.route("/t/e/<int:event_id>/reg/<int:reg_id>")
@event_participant_required
def t_child(event, reg_id):
    reg = scoped_get_or_404(ArenaRegistration, reg_id, event)
    return jsonify({"event": event.id, "reg": reg.id})


@bp.route("/t/global/create")
@global_permission_required("events.create")
def t_global_create():
    return jsonify({"ok": True})


@bp.route("/t/broken")
@event_view
def t_broken(event):  # pragma: no cover - must never run
    return "unreachable"


EVENTS = {
    "A": {"status": "running", "visibility": "public"},
    "B": {"status": "running", "visibility": "private"},
    "C": {"status": "draft", "visibility": "public"},
    "D": {"status": "running", "visibility": "unlisted"},
}

# actor -> (registrations {event: status}, roles [(role, event or None)], admin?)
ACTORS = {
    "anon": None,
    "outsider": ({}, [], False),
    "pending_A": ({"A": "pending"}, [], False),
    "member_A": ({"A": "approved"}, [], False),
    "member_B": ({"B": "approved"}, [], False),
    "member_all": (dict.fromkeys(EVENTS, "approved"), [], False),
    "regbanned_A": ({"A": "banned"}, [], False),
    "eadmin_A": ({}, [("event_admin", "A")], False),
    "auditor_A": ({}, [("auditor", "A")], False),
    "g_auditor": ({}, [("auditor", None)], False),
    "superadmin": ({}, [], True),
}
ROUTES = ("view", "part", "manage", "audit")
PERMS = {
    "manage": {"event_admin"},
    "audit": {"event_admin", "auditor"},
}


def expected(actor, ev, route):
    """Reference oracle derived from the security plan."""
    if actor == "anon":
        if route != "view":
            return 401
        visible = (
            EVENTS[ev]["status"] != "draft" and EVENTS[ev]["visibility"] != "private"
        )
        return 200 if visible else 404
    regs, roles, is_admin = ACTORS[actor]
    roles_here = {r for (r, e) in roles if e in (ev, None)}
    privileged = is_admin or bool(roles_here)
    if privileged:
        visible = True
    elif EVENTS[ev]["status"] == "draft":
        visible = False
    elif EVENTS[ev]["visibility"] in ("public", "unlisted"):
        visible = True
    else:
        visible = regs.get(ev) in ("pending", "approved")
    if not visible:
        return 404
    if route == "view":
        return 200
    if route == "part":
        return 200 if regs.get(ev) == "approved" else 403
    allowed = is_admin or bool(roles_here & PERMS[route])
    return 200 if allowed else 403


@pytest.fixture(scope="module")
def world():
    app = create_arena_ctfd(blueprints=[bp])
    # ~15 distinct actors log in within seconds; native login limit is 10/5s.
    app.config["RATELIMIT_LOGIN"] = (100, 5)
    ids = {}
    with app.app_context():
        for key, kw in EVENTS.items():
            ids[key] = gen_event(slug=f"ev-{key.lower()}", **kw).id
        users = {}
        for actor, spec in ACTORS.items():
            if spec is None or actor == "superadmin":
                continue
            regs, roles, _ = spec
            uid = make_user(actor)
            users[actor] = uid
            for ev, status in regs.items():
                gen_registration(ids[ev], uid, status=status)
            for role, ev in roles:
                gen_role(uid, role, event_id=ids[ev] if ev else None)
    yield app, ids, users
    destroy_ctfd(app)


_CLIENTS = {}


def client_for(app, actor):
    """One login per actor per app: native login is rate limited."""
    if actor == "anon":
        return app.test_client()
    key = (id(app), actor)
    if key not in _CLIENTS:
        name = "admin" if actor == "superadmin" else actor
        _CLIENTS[key] = login_as_user(app, name=name, password="password")
    return _CLIENTS[key]


CASES = [(a, e, r) for a in ACTORS for e in EVENTS for r in ROUTES]


@pytest.mark.parametrize(
    "actor,ev,route", CASES, ids=[f"{a}-{e}-{r}" for a, e, r in CASES]
)
def test_guard_matrix(world, actor, ev, route):
    app, ids, _ = world
    client = client_for(app, actor)
    r = client.get(f"/t/e/{ids[ev]}/{route}")
    assert r.status_code == expected(actor, ev, route), r.get_data(as_text=True)[:200]
    if r.status_code == 200:
        assert r.get_json()["event"] == ids[ev]


def test_nonexistent_event_is_404_for_everyone(world):
    app, _, _ = world
    for actor in ("anon", "member_all", "superadmin"):
        c = client_for(app, actor)
        assert c.get("/t/e/999999/view").status_code == 404
    c = client_for(app, "superadmin")
    assert c.get("/t/e/999999/manage").status_code == 404


def test_non_integer_event_id_is_404(world):
    app, _, _ = world
    c = client_for(app, "member_all")
    assert c.get("/t/e/abc/view").status_code == 404
    assert c.get("/t/e/1%20OR%201=1/view").status_code == 404


def test_event_id_in_query_or_body_is_ignored(world):
    """IDOR: a member of B cannot reach A by smuggling event_id elsewhere."""
    app, ids, _ = world
    c = client_for(app, "member_B")
    smuggled = f"/t/e/{ids['A']}/part?event_id={ids['B']}"
    r = c.post(smuggled, json={"event_id": ids["B"]})
    assert r.status_code == 403  # resolved from URL => A, not registered
    r = c.post(f"/t/e/{ids['B']}/part?event_id={ids['A']}", json={"event_id": ids["A"]})
    assert r.status_code == 200
    assert r.get_json()["event"] == ids["B"]  # handler only ever sees URL event


def test_event_admin_cannot_manage_other_event_by_smuggling(world):
    app, ids, _ = world
    c = client_for(app, "eadmin_A")
    r = c.post(
        f"/t/e/{ids['D']}/manage?event_id={ids['A']}", json={"event_id": ids["A"]}
    )
    assert r.status_code == 403


def test_child_object_from_other_event_is_404(world):
    app, ids, users = world
    with app.app_context():
        reg_b = ArenaRegistration.query.filter_by(
            event_id=ids["B"], user_id=users["member_all"]
        ).first()
        reg_a = ArenaRegistration.query.filter_by(
            event_id=ids["A"], user_id=users["member_all"]
        ).first()
    c = client_for(app, "member_all")
    assert c.get(f"/t/e/{ids['A']}/reg/{reg_a.id}").status_code == 200
    # Same user, valid object id, wrong event in URL => not found.
    assert c.get(f"/t/e/{ids['A']}/reg/{reg_b.id}").status_code == 404


def test_global_permission_superadmin_only(world):
    app, _, _ = world
    assert client_for(app, "anon").get("/t/global/create").status_code == 401
    for actor in ("member_all", "eadmin_A", "g_auditor"):
        assert client_for(app, actor).get("/t/global/create").status_code == 403
    assert client_for(app, "superadmin").get("/t/global/create").status_code == 200


def test_natively_banned_user_denied_everywhere(world):
    app, ids, _ = world
    with app.app_context():
        uid = make_user("tobebanned")
        gen_registration(ids["A"], uid, status="approved")
    c = client_for(app, "tobebanned")
    assert c.get(f"/t/e/{ids['A']}/part").status_code == 200
    with app.app_context():
        Users.query.filter_by(id=uid).update({"banned": True})
        db.session.commit()
        clear_user_session(user_id=uid)
    for route in ROUTES:
        assert c.get(f"/t/e/{ids['A']}/{route}").status_code == 403


def test_revoked_registration_takes_effect_immediately(world):
    """Authorization is read from the DB on every request, not the session."""
    app, ids, _ = world
    with app.app_context():
        uid = make_user("revokee")
        reg_id = gen_registration(ids["A"], uid, status="approved").id
    c = client_for(app, "revokee")
    assert c.get(f"/t/e/{ids['A']}/part").status_code == 200
    with app.app_context():
        ArenaRegistration.query.filter_by(id=reg_id).update({"status": "banned"})
        db.session.commit()
    assert c.get(f"/t/e/{ids['A']}/part").status_code == 403


def test_guard_without_event_id_fails_closed(world):
    app, _, _ = world
    app.config["PROPAGATE_EXCEPTIONS"] = True
    try:
        with pytest.raises(GuardMisconfigured):
            client_for(app, "member_all").get("/t/broken")
    finally:
        app.config["PROPAGATE_EXCEPTIONS"] = None


def test_unknown_permission_rejected_at_decoration_time():
    with pytest.raises(authz.rbac.UnknownPermission):
        event_permission_required("event.mange")  # typo
    with pytest.raises(authz.rbac.UnknownPermission):
        global_permission_required("nope")


def test_resolved_event_exposed_on_g(world):
    app, ids, _ = world
    with app.test_request_context(f"/t/e/{ids['A']}/view"):
        from CTFd.plugins.arena.core.context import current_event, resolve_event

        resolve_event(ids["A"])
        assert g.arena_event.id == ids["A"]
        assert current_event().id == ids["A"]


def test_native_csrf_protects_arena_routes(world):
    """POSTs without the session nonce are rejected before Arena code runs."""
    app, ids, _ = world
    c = client_for(app, "member_all")
    # Form POST without nonce / CSRF-Token header.
    r = c.post(f"/t/e/{ids['A']}/part", data={"x": "1"})
    assert r.status_code == 403
    # Wrong CSRF-Token header.
    r = c.open(
        f"/t/e/{ids['A']}/part",
        method="POST",
        data="{}",
        content_type="application/json",
        headers={"CSRF-Token": "forged"},
    )
    assert r.status_code == 403
    # Correct token (test client adds it for json=) succeeds.
    assert c.post(f"/t/e/{ids['A']}/part", json={}).status_code == 200

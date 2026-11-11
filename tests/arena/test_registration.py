"""Phase 2: registration lifecycle, eligibility, invites and capacity."""

import datetime
import threading

import pytest

from CTFd.models import Users, db
from CTFd.plugins.arena import integrity
from CTFd.plugins.arena.models import ArenaEvent, ArenaRegistration
from CTFd.plugins.arena.services import registration as reg_service
from CTFd.utils import set_config
from tests.arena.helpers import (
    create_arena_ctfd,
    gen_event,
    gen_registration,
    make_user,
)
from tests.helpers import destroy_ctfd, login_as_user

API = "/api/v1/arena"
NOW = datetime.datetime.utcnow()


@pytest.fixture
def app():
    app = create_arena_ctfd()
    app.config["RATELIMIT_LOGIN"] = (1000, 5)
    yield app
    destroy_ctfd(app)


def ev(app, slug="e", **kw):
    kw.setdefault("registration_open", True)
    with app.app_context():
        return gen_event(slug, **kw).id


def user_client(app, name, **kw):
    with app.app_context():
        uid = make_user(name, **kw)
    return uid, login_as_user(app, name=name)


def seats(app, eid):
    with app.app_context():
        return db.session.get(ArenaEvent, eid).seat_count


def test_register_and_withdraw_and_reregister(app):
    eid = ev(app)
    uid, c = user_client(app, "u")
    r = c.post(f"{API}/events/{eid}/register", json={})
    assert r.status_code == 201 and r.get_json()["data"]["status"] == "approved"
    assert seats(app, eid) == 1
    assert (
        c.get(f"{API}/events/{eid}").get_json()["data"]["my_registration"] == "approved"
    )
    r = c.post(f"{API}/events/{eid}/register", json={})
    assert r.status_code == 409 and r.get_json()["code"] == "already_registered"
    assert c.post(f"{API}/events/{eid}/withdraw", json={}).status_code == 200
    assert seats(app, eid) == 0
    assert c.post(f"{API}/events/{eid}/register", json={}).status_code == 201
    assert seats(app, eid) == 1
    assert [e["id"] for e in c.get(f"{API}/me/events").get_json()["data"]] == [eid]


def test_anonymous_cannot_register(app):
    eid = ev(app)
    assert app.test_client().post(
        f"{API}/events/{eid}/register", json={}
    ).status_code in (401, 403)


@pytest.mark.parametrize(
    "kw,code",
    [
        ({"registration_open": False}, "registration_closed"),
        (
            {"registration_opens_at": NOW + datetime.timedelta(hours=1)},
            "registration_closed",
        ),
        (
            {"registration_closes_at": NOW - datetime.timedelta(minutes=1)},
            "registration_closed",
        ),
        ({"status": "completed"}, "registration_closed"),
        ({"status": "paused"}, "registration_closed"),
        (
            {
                "ends_at": NOW - datetime.timedelta(minutes=1),
                "starts_at": NOW - datetime.timedelta(hours=2),
            },
            "registration_closed",
        ),
        ({"settings": {"allowed_email_domains": ["uni.edu"]}}, "ineligible"),
        ({"settings": {"invite_only": True}}, "invite_required"),
    ],
)
def test_registration_rules_enforced_server_side(app, kw, code):
    eid = ev(app, **kw)
    _, c = user_client(app, "u")
    r = c.post(f"{API}/events/{eid}/register", json={})
    assert r.get_json()["code"] == code, r.get_json()
    assert seats(app, eid) == 0


def test_draft_event_cannot_be_registered(app):
    eid = ev(app, status="draft")
    _, c = user_client(app, "u")
    assert c.post(f"{API}/events/{eid}/register", json={}).status_code == 404


def test_email_domain_allowlist_accepts_matching(app):
    eid = ev(app, settings={"allowed_email_domains": ["uni.edu"]})
    _, c = user_client(app, "u", email="u@uni.edu")
    assert c.post(f"{API}/events/{eid}/register", json={}).status_code == 201


def test_verified_email_required(app):
    eid = ev(app, settings={"require_verified_email": True})
    _, c = user_client(app, "u", verified=False)
    assert (
        c.post(f"{API}/events/{eid}/register", json={}).get_json()["code"]
        == "unverified"
    )
    with app.app_context():
        set_config("verify_emails", False)


def test_approval_flow(app):
    eid = ev(app, requires_approval=True, max_participants=1)
    uid, c = user_client(app, "u")
    assert (
        c.post(f"{API}/events/{eid}/register", json={}).get_json()["data"]["status"]
        == "pending"
    )
    assert seats(app, eid) == 1  # pending holds a seat
    admin = login_as_user(app, name="admin")
    with app.app_context():
        rid = ArenaRegistration.query.filter_by(user_id=uid).first().id
    r = admin.post(f"{API}/admin/events/{eid}/registrations/{rid}/approve", json={})
    assert r.status_code == 200 and r.get_json()["data"]["status"] == "approved"
    assert (
        admin.post(
            f"{API}/admin/events/{eid}/registrations/{rid}/approve", json={}
        ).status_code
        == 409
    )
    assert (
        admin.post(
            f"{API}/admin/events/{eid}/registrations/{rid}/ban", json={}
        ).status_code
        == 200
    )
    assert seats(app, eid) == 0
    # banned users cannot self-register again
    assert (
        c.post(f"{API}/events/{eid}/register", json={}).get_json()["code"]
        == "not_allowed"
    )
    assert (
        admin.post(
            f"{API}/admin/events/{eid}/registrations/{rid}/unban", json={}
        ).status_code
        == 200
    )
    assert seats(app, eid) == 1
    assert (
        admin.post(
            f"{API}/admin/events/{eid}/registrations/{rid}/explode", json={}
        ).status_code
        == 400
    )


def test_reject_releases_seat(app):
    eid = ev(app, requires_approval=True, max_participants=1)
    uid, c = user_client(app, "u")
    c.post(f"{API}/events/{eid}/register", json={})
    _, c2 = user_client(app, "u2")
    assert (
        c2.post(f"{API}/events/{eid}/register", json={}).get_json()["code"]
        == "event_full"
    )
    admin = login_as_user(app, name="admin")
    with app.app_context():
        rid = ArenaRegistration.query.filter_by(user_id=uid).first().id
    admin.post(f"{API}/admin/events/{eid}/registrations/{rid}/reject", json={})
    assert c2.post(f"{API}/events/{eid}/register", json={}).status_code == 201


def test_capacity_and_admin_enroll(app):
    eid = ev(app, max_participants=2)
    for n in ("a", "b"):
        _, c = user_client(app, n)
        assert c.post(f"{API}/events/{eid}/register", json={}).status_code == 201
    uid, c = user_client(app, "c")
    r = c.post(f"{API}/events/{eid}/register", json={})
    assert r.status_code == 409 and r.get_json()["code"] == "event_full"
    admin = login_as_user(app, name="admin")
    assert (
        admin.post(
            f"{API}/admin/events/{eid}/registrations", json={"user_id": uid}
        ).status_code
        == 409
    )
    r = admin.post(
        f"{API}/admin/events/{eid}/registrations", json={"user_id": uid, "force": True}
    )
    assert r.status_code == 201
    assert seats(app, eid) == 3
    with app.app_context():
        assert integrity.check_seat_counts() == [
            f"event {eid}: 3 seats held exceeds max_participants=2"
        ]


def test_private_event_requires_invite_and_join_flow(app):
    eid = ev(app, visibility="private")
    admin = login_as_user(app, name="admin")
    r = admin.post(
        f"{API}/admin/events/{eid}/invites", json={"label": "uni", "max_uses": 1}
    )
    assert r.status_code == 201
    code = r.get_json()["data"]["code"]
    _, c = user_client(app, "u")
    # Not visible without registration -> 404 on the id route.
    assert (
        c.post(f"{API}/events/{eid}/register", json={"invite_code": code}).status_code
        == 404
    )
    assert (
        c.post(f"{API}/events/join", json={"invite_code": "wrong"}).status_code == 403
    )
    r = c.post(f"{API}/events/join", json={"invite_code": code})
    assert r.status_code == 201, r.get_json()
    assert c.get(f"{API}/events/{eid}").status_code == 200  # now visible
    _, c2 = user_client(app, "u2")
    # max_uses=1 exhausted
    assert c2.post(f"{API}/events/join", json={"invite_code": code}).status_code == 403
    invites = admin.get(f"{API}/admin/events/{eid}/invites").get_json()["data"]
    assert invites[0]["uses"] == 1 and "code" not in invites[0]


def test_revoked_and_expired_invites(app):
    eid = ev(app, visibility="private")
    admin = login_as_user(app, name="admin")
    inv = admin.post(f"{API}/admin/events/{eid}/invites", json={}).get_json()["data"]
    assert (
        admin.delete(
            f"{API}/admin/events/{eid}/invites/{inv['id']}", json={}
        ).status_code
        == 200
    )
    _, c = user_client(app, "u")
    assert (
        c.post(f"{API}/events/join", json={"invite_code": inv["code"]}).status_code
        == 403
    )
    past = (NOW - datetime.timedelta(minutes=1)).isoformat() + "Z"
    inv2 = admin.post(
        f"{API}/admin/events/{eid}/invites", json={"expires_at": past}
    ).get_json()["data"]
    assert (
        c.post(f"{API}/events/join", json={"invite_code": inv2["code"]}).status_code
        == 403
    )


def test_invite_from_other_event_rejected(app):
    a = ev(app, "a", settings={"invite_only": True})
    b = ev(app, "b", settings={"invite_only": True})
    admin = login_as_user(app, name="admin")
    code_b = admin.post(f"{API}/admin/events/{b}/invites", json={}).get_json()["data"][
        "code"
    ]
    _, c = user_client(app, "u")
    r = c.post(f"{API}/events/{a}/register", json={"invite_code": code_b})
    assert r.get_json()["code"] == "invite_required"


def test_cross_event_registration_admin_isolation(app):
    a = ev(app, "a")
    b = ev(app, "b")
    with app.app_context():
        uid = make_user("u")
        reg_b = gen_registration(b, uid).id
        org = make_user("org")
        from tests.arena.helpers import gen_role

        gen_role(org, "organizer", a)
    c = login_as_user(app, name="org")
    # Registration of event B addressed through event A's URL -> 404
    assert (
        c.post(f"{API}/admin/events/{a}/registrations/{reg_b}/ban", json={}).status_code
        == 404
    )
    assert c.get(f"{API}/admin/events/{b}/registrations").status_code == 403


def _concurrent(app, eid, user_ids, fn):
    """Run fn(event, user) in parallel threads, released together.

    The barrier is passed BEFORE any DB access: threads that grabbed pooled
    connections while waiting would otherwise starve the pool (deadlock).
    """
    barrier = threading.Barrier(len(user_ids), timeout=30)
    results = []
    lock = threading.Lock()

    def worker(uid):
        barrier.wait()
        with app.app_context():
            event = db.session.get(ArenaEvent, eid)
            user = db.session.get(Users, uid)
            try:
                fn(event, user)
                out = "ok"
            except Exception as e:  # noqa: BLE001
                out = getattr(e, "code", None) or type(e).__name__
            finally:
                db.session.remove()
            with lock:
                results.append(out)

    threads = [threading.Thread(target=worker, args=(u,)) for u in user_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads), "concurrency test hung"
    return results


def _is_server_db(app):
    return not app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite")


def test_concurrent_registration_never_exceeds_capacity(app):
    if not _is_server_db(app):
        pytest.skip("real concurrency needs MariaDB/MySQL (TESTING_DATABASE_URL)")
    eid = ev(app, max_participants=5)
    with app.app_context():
        uids = [make_user(f"c{i}") for i in range(40)]
    results = _concurrent(app, eid, uids, lambda e, u: reg_service.register(e, u))
    assert results.count("ok") == 5, results
    assert results.count("event_full") == 35, results
    with app.app_context():
        assert db.session.get(ArenaEvent, eid).seat_count == 5
        assert reg_service.recount_seats(eid) == 5
        assert integrity.check_seat_counts() == []


def test_concurrent_duplicate_registration_same_user(app):
    if not _is_server_db(app):
        pytest.skip("real concurrency needs MariaDB/MySQL (TESTING_DATABASE_URL)")
    eid = ev(app, max_participants=100)
    with app.app_context():
        uid = make_user("dup")
    results = _concurrent(app, eid, [uid] * 20, lambda e, u: reg_service.register(e, u))
    assert results.count("ok") == 1, results
    with app.app_context():
        assert ArenaRegistration.query.filter_by(event_id=eid, user_id=uid).count() == 1
        assert db.session.get(ArenaEvent, eid).seat_count == 1


def test_concurrent_ban_releases_seat_once(app):
    if not _is_server_db(app):
        pytest.skip("real concurrency needs MariaDB/MySQL (TESTING_DATABASE_URL)")
    eid = ev(app, max_participants=10)
    with app.app_context():
        uids = [make_user(f"s{i}") for i in range(3)]
        for u in uids:
            reg_service.register(
                db.session.get(ArenaEvent, eid), db.session.get(Users, u)
            )
        target = ArenaRegistration.query.filter_by(user_id=uids[0]).first().id
        admin = Users.query.filter_by(type="admin").first().id

    def ban(e, _u):
        reg = db.session.get(ArenaRegistration, target)
        reg_service.admin_action(e, reg, "ban", db.session.get(Users, admin))

    results = _concurrent(app, eid, [admin] * 10, ban)
    assert results.count("ok") == 1, results
    with app.app_context():
        assert db.session.get(ArenaEvent, eid).seat_count == 2
        assert integrity.check_seat_counts() == []

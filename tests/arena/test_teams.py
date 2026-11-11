"""Phase 3: event teams."""

import datetime
import threading

import pytest

from CTFd.models import Users, db
from CTFd.plugins.arena import integrity
from CTFd.plugins.arena.models import ArenaEvent, ArenaTeam, ArenaTeamMember
from CTFd.plugins.arena.services import teams as team_service
from tests.arena.helpers import (
    create_arena_ctfd,
    gen_event,
    gen_registration,
    gen_role,
    make_user,
)
from tests.helpers import destroy_ctfd, login_as_user

API = "/api/v1/arena"


@pytest.fixture
def app():
    app = create_arena_ctfd()
    app.config["RATELIMIT_LOGIN"] = (1000, 5)
    yield app
    destroy_ctfd(app)


def team_event(app, slug="t", size=3, **kw):
    with app.app_context():
        return gen_event(
            slug, team_mode="team", team_size_min=1, team_size_max=size, **kw
        ).id


_clients = {}


def player(app, name, *event_ids, status="approved"):
    with app.app_context():
        uid = make_user(name)
        for eid in event_ids:
            gen_registration(eid, uid, status=status)
    return uid, login_as_user(app, name=name)


def create(c, eid, name="Red", **kw):
    r = c.post(f"{API}/events/{eid}/teams", json={"name": name, **kw})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["data"]


def test_create_join_leave_lifecycle(app):
    eid = team_event(app)
    a, ca = player(app, "alice", eid)
    b, cb = player(app, "bob", eid)
    t = create(ca, eid)
    assert t["member_count"] == 1 and t["captain_user_id"] == a and t["join_code"]
    r = cb.post(f"{API}/events/{eid}/teams/join", json={"join_code": t["join_code"]})
    assert r.status_code == 200 and r.get_json()["data"]["member_count"] == 2
    assert (
        cb.post(f"{API}/events/{eid}/teams", json={"name": "Blue"}).get_json()["code"]
        == "already_in_team"
    )
    mine = cb.get(f"{API}/events/{eid}/my-team").get_json()["data"]
    assert mine["id"] == t["id"] and "pending_invites" not in mine  # not captain
    # captain must transfer before leaving
    assert (
        ca.post(f"{API}/events/{eid}/my-team/leave", json={}).get_json()["code"]
        == "captain_must_transfer"
    )
    assert (
        ca.post(f"{API}/events/{eid}/my-team/captain", json={"user_id": b}).status_code
        == 200
    )
    assert ca.post(f"{API}/events/{eid}/my-team/leave", json={}).status_code == 200
    assert cb.post(f"{API}/events/{eid}/my-team/leave", json={}).status_code == 200
    with app.app_context():
        assert (
            db.session.get(ArenaTeam, t["id"]) is None
        )  # empty team without history is removed
        assert integrity.check_teams() == []


def test_team_size_and_individual_mode(app):
    eid = team_event(app, size=2)
    _, c1 = player(app, "u1", eid)
    _, c2 = player(app, "u2", eid)
    _, c3 = player(app, "u3", eid)
    code = create(c1, eid)["join_code"]
    assert (
        c2.post(f"{API}/events/{eid}/teams/join", json={"join_code": code}).status_code
        == 200
    )
    r = c3.post(f"{API}/events/{eid}/teams/join", json={"join_code": code})
    assert r.get_json()["code"] == "team_full"
    with app.app_context():
        ind = gen_event("ind").id
    _, c4 = player(app, "u4", ind)
    r = c4.post(f"{API}/events/{ind}/teams", json={"name": "Solo"})
    assert r.get_json()["code"] == "not_team_event"


def test_requires_approved_registration(app):
    eid = team_event(app)
    _, c = player(app, "pending", eid, status="pending")
    assert c.post(f"{API}/events/{eid}/teams", json={"name": "X"}).status_code == 403
    _, c2 = player(app, "outsider")
    assert c2.post(f"{API}/events/{eid}/teams", json={"name": "X"}).status_code == 403


def test_name_validation_and_uniqueness_per_event(app):
    e1 = team_event(app, "e1")
    e2 = team_event(app, "e2")
    _, ca = player(app, "a", e1, e2)
    _, cb = player(app, "b", e1)
    assert ca.post(f"{API}/events/{e1}/teams", json={"name": "x"}).status_code == 400
    assert (
        ca.post(f"{API}/events/{e1}/teams", json={"name": "bad\x00name"}).status_code
        == 400
    )
    create(ca, e1, "Same")
    assert (
        cb.post(f"{API}/events/{e1}/teams", json={"name": "Same"}).get_json()["code"]
        == "name_taken"
    )
    create(ca, e2, "Same")  # same name in another event is fine


def test_user_in_different_teams_in_different_events(app):
    e1 = team_event(app, "e1")
    e2 = team_event(app, "e2")
    a, ca = player(app, "a", e1, e2)
    t1 = create(ca, e1, "Alpha")
    t2 = create(ca, e2, "Beta")
    assert t1["id"] != t2["id"]
    assert ca.get(f"{API}/events/{e1}/my-team").get_json()["data"]["name"] == "Alpha"
    assert ca.get(f"{API}/events/{e2}/my-team").get_json()["data"]["name"] == "Beta"
    with app.app_context():
        assert db.session.get(Users, a).team_id is None  # native team untouched


def test_join_code_scoped_to_event(app):
    e1 = team_event(app, "e1")
    e2 = team_event(app, "e2")
    _, ca = player(app, "a", e1)
    _, cb = player(app, "b", e2)
    code = create(ca, e1)["join_code"]
    r = cb.post(f"{API}/events/{e2}/teams/join", json={"join_code": code})
    assert r.get_json()["code"] == "invalid_code"


def test_regenerate_join_code_invalidates_old(app):
    eid = team_event(app)
    _, ca = player(app, "a", eid)
    _, cb = player(app, "b", eid)
    old = create(ca, eid)["join_code"]
    new = ca.post(f"{API}/events/{eid}/my-team/join-code", json={}).get_json()["data"][
        "join_code"
    ]
    assert (
        cb.post(f"{API}/events/{eid}/teams/join", json={"join_code": old}).status_code
        == 403
    )
    assert (
        cb.post(f"{API}/events/{eid}/teams/join", json={"join_code": new}).status_code
        == 200
    )
    # non-captain cannot regenerate
    assert (
        cb.post(f"{API}/events/{eid}/my-team/join-code", json={}).get_json()["code"]
        == "not_captain"
    )


def test_invites_accept_reject_cancel(app):
    eid = team_event(app)
    a, ca = player(app, "a", eid)
    b, cb = player(app, "b", eid)
    c, cc = player(app, "c", eid)
    create(ca, eid)
    inv = ca.post(
        f"{API}/events/{eid}/my-team/invites", json={"user_id": b}
    ).get_json()["data"]
    assert (
        ca.post(f"{API}/events/{eid}/my-team/invites", json={"user_id": b}).get_json()[
            "code"
        ]
        == "already_invited"
    )
    assert [
        i["id"] for i in cb.get(f"{API}/events/{eid}/my-invites").get_json()["data"]
    ] == [inv["id"]]
    # c cannot answer b's invite
    assert (
        cc.post(
            f"{API}/events/{eid}/my-invites/{inv['id']}/accept", json={}
        ).status_code
        == 404
    )
    assert (
        cb.post(
            f"{API}/events/{eid}/my-invites/{inv['id']}/accept", json={}
        ).status_code
        == 200
    )
    assert (
        cb.post(
            f"{API}/events/{eid}/my-invites/{inv['id']}/accept", json={}
        ).status_code
        == 409
    )
    inv2 = ca.post(
        f"{API}/events/{eid}/my-team/invites", json={"user_id": c}
    ).get_json()["data"]
    assert (
        cc.post(
            f"{API}/events/{eid}/my-invites/{inv2['id']}/reject", json={}
        ).status_code
        == 200
    )
    inv3 = ca.post(
        f"{API}/events/{eid}/my-team/invites", json={"user_id": c}
    ).get_json()["data"]
    assert (
        ca.delete(
            f"{API}/events/{eid}/my-team/invites/{inv3['id']}", json={}
        ).status_code
        == 200
    )
    assert (
        cc.post(
            f"{API}/events/{eid}/my-invites/{inv3['id']}/accept", json={}
        ).status_code
        == 409
    )


def test_joining_a_team_voids_other_pending_invites(app):
    eid = team_event(app)
    _, ca = player(app, "a", eid)
    _, cz = player(app, "z", eid)
    b, cb = player(app, "b", eid)
    create(ca, eid, "Aa")
    code_z = create(cz, eid, "Zz")["join_code"]
    ca.post(f"{API}/events/{eid}/my-team/invites", json={"user_id": b})
    cb.post(f"{API}/events/{eid}/teams/join", json={"join_code": code_z})
    assert cb.get(f"{API}/events/{eid}/my-invites").get_json()["data"] == []


def test_kick_and_private_team_visibility(app):
    eid = team_event(app)
    a, ca = player(app, "a", eid)
    b, cb = player(app, "b", eid)
    _, cx = player(app, "x", eid)
    t = create(ca, eid, visibility="private")
    cb.post(f"{API}/events/{eid}/teams/join", json={"join_code": t["join_code"]})
    assert cx.get(f"{API}/events/{eid}/teams/{t['id']}").status_code == 404
    assert cb.get(f"{API}/events/{eid}/teams/{t['id']}").status_code == 200
    assert cx.get(f"{API}/events/{eid}/teams").get_json()["data"] == []
    assert (
        cb.post(f"{API}/events/{eid}/my-team/members/{a}/remove", json={}).get_json()[
            "code"
        ]
        == "not_captain"
    )
    assert (
        ca.post(f"{API}/events/{eid}/my-team/members/{b}/remove", json={}).status_code
        == 200
    )
    assert cb.get(f"{API}/events/{eid}/my-team").get_json()["data"] is None


def test_locking_after_start(app):
    now = datetime.datetime.utcnow()
    eid = team_event(
        app,
        settings={"lock_teams_after_start": True},
        starts_at=now - datetime.timedelta(minutes=5),
    )
    _, ca = player(app, "a", eid)
    r = ca.post(f"{API}/events/{eid}/teams", json={"name": "Late"})
    assert r.get_json()["code"] == "teams_locked"


def test_completed_event_locks_membership(app):
    eid = team_event(app)
    _, ca = player(app, "a", eid)
    create(ca, eid)
    with app.app_context():
        db.session.get(ArenaEvent, eid).status = "completed"
        db.session.commit()
    assert (
        ca.post(f"{API}/events/{eid}/my-team/leave", json={}).get_json()["code"]
        == "teams_locked"
    )


def test_banned_team_cannot_evade(app):
    eid = team_event(app)
    _, ca = player(app, "a", eid)
    _, cb = player(app, "b", eid)
    t = create(ca, eid)
    cb.post(f"{API}/events/{eid}/teams/join", json={"join_code": t["join_code"]})
    admin = login_as_user(app, name="admin")
    assert (
        admin.post(f"{API}/admin/events/{eid}/teams/{t['id']}/ban", json={}).status_code
        == 200
    )
    assert (
        cb.post(f"{API}/events/{eid}/my-team/leave", json={}).get_json()["code"]
        == "team_banned"
    )
    _, cc = player(app, "c", eid)
    code2 = create(cc, eid, "Other")["join_code"]
    assert (
        cb.post(f"{API}/events/{eid}/teams/join", json={"join_code": code2}).get_json()[
            "code"
        ]
        == "team_banned"
    )


def test_registration_withdrawal_removes_membership_with_succession(app):
    eid = team_event(app)
    a, ca = player(app, "a", eid)
    b, cb = player(app, "b", eid)
    t = create(ca, eid)
    cb.post(f"{API}/events/{eid}/teams/join", json={"join_code": t["join_code"]})
    assert ca.post(f"{API}/events/{eid}/withdraw", json={}).status_code == 200
    with app.app_context():
        team = db.session.get(ArenaTeam, t["id"])
        assert team.captain_user_id == b and team.member_count == 1
        assert integrity.check_teams() == []


def test_admin_team_management_and_isolation(app):
    e1 = team_event(app, "e1")
    e2 = team_event(app, "e2")
    a, ca = player(app, "a", e1)
    b, _ = player(app, "b", e1)
    t = create(ca, e1)
    with app.app_context():
        org = make_user("org2")
        gen_role(org, "organizer", e2)
        ana = make_user("ana")
        gen_role(ana, "analyst", e1)
    org_c = login_as_user(app, name="org2")
    # Team of e1 addressed through e2 -> 404; e1 admin routes -> 403
    assert (
        org_c.post(f"{API}/admin/events/{e2}/teams/{t['id']}/ban", json={}).status_code
        == 404
    )
    assert org_c.get(f"{API}/admin/events/{e1}/teams").status_code == 403
    ana_c = login_as_user(app, name="ana")
    assert ana_c.get(f"{API}/admin/events/{e1}/teams").status_code == 200
    assert (
        ana_c.post(f"{API}/admin/events/{e1}/teams/{t['id']}/ban", json={}).status_code
        == 403
    )
    admin = login_as_user(app, name="admin")
    r = admin.post(
        f"{API}/admin/events/{e1}/teams/{t['id']}/members", json={"user_id": b}
    )
    assert r.status_code == 200 and r.get_json()["data"]["member_count"] == 2
    assert (
        admin.post(
            f"{API}/admin/events/{e1}/teams/{t['id']}/captain", json={"user_id": b}
        ).status_code
        == 200
    )
    assert (
        admin.post(
            f"{API}/admin/events/{e1}/teams/{t['id']}/members/{b}/remove", json={}
        ).status_code
        == 200
    )
    with app.app_context():
        team = db.session.get(ArenaTeam, t["id"])
        assert team.captain_user_id == a  # succession back to remaining member
    assert (
        admin.post(
            f"{API}/admin/events/{e1}/teams/{t['id']}/disband", json={}
        ).status_code
        == 200
    )
    with app.app_context():
        assert ArenaTeamMember.query.filter_by(event_id=e1).count() == 0
        assert integrity.check_teams() == []


def _server_db(app):
    return not app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite")


def _parallel(app, n, fn):
    barrier = threading.Barrier(n, timeout=30)
    out, lock = [], threading.Lock()

    def run(i):
        barrier.wait()
        with app.app_context():
            try:
                fn(i)
                r = "ok"
            except Exception as e:  # noqa: BLE001
                r = getattr(e, "code", None) or type(e).__name__
            finally:
                db.session.remove()
        with lock:
            out.append(r)

    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join(timeout=120) for t in ts]
    assert not any(t.is_alive() for t in ts), "hung"
    return out


def test_concurrent_joins_respect_team_size(app):
    if not _server_db(app):
        pytest.skip("needs MariaDB/MySQL")
    eid = team_event(app, size=4)
    with app.app_context():
        uids = [make_user(f"j{i}") for i in range(25)]
        for u in uids:
            gen_registration(eid, u)
        event = db.session.get(ArenaEvent, eid)
        _team, code = team_service.create_team(
            event, db.session.get(Users, uids[0]), {"name": "Race"}
        )

    def join(i):
        event = db.session.get(ArenaEvent, eid)
        team_service.join_with_code(event, db.session.get(Users, uids[i + 1]), code)

    res = _parallel(app, 24, join)
    assert res.count("ok") == 3, res  # captain + 3 = size 4
    with app.app_context():
        assert ArenaTeamMember.query.filter_by(event_id=eid).count() == 4
        assert integrity.check_teams() == []


def test_concurrent_one_team_per_user(app):
    if not _server_db(app):
        pytest.skip("needs MariaDB/MySQL")
    eid = team_event(app, size=10)
    with app.app_context():
        uid = make_user("dual")
        gen_registration(eid, uid)
        codes = []
        for i in range(10):
            cap = make_user(f"cap{i}")
            gen_registration(eid, cap)
            event = db.session.get(ArenaEvent, eid)
            codes.append(
                team_service.create_team(
                    event, db.session.get(Users, cap), {"name": f"T{i}"}
                )[1]
            )

    def join(i):
        event = db.session.get(ArenaEvent, eid)
        team_service.join_with_code(event, db.session.get(Users, uid), codes[i])

    res = _parallel(app, 10, join)
    assert res.count("ok") == 1, res
    with app.app_context():
        assert ArenaTeamMember.query.filter_by(event_id=eid, user_id=uid).count() == 1
        assert integrity.check_teams() == []


def test_concurrent_captain_transfers(app):
    if not _server_db(app):
        pytest.skip("needs MariaDB/MySQL")
    eid = team_event(app, size=10)
    with app.app_context():
        uids = [make_user(f"m{i}") for i in range(6)]
        for u in uids:
            gen_registration(eid, u)
        event = db.session.get(ArenaEvent, eid)
        team, code = team_service.create_team(
            event, db.session.get(Users, uids[0]), {"name": "Cap"}
        )
        tid = team.id
        for u in uids[1:]:
            team_service.join_with_code(event, db.session.get(Users, u), code)

    def transfer(i):
        event = db.session.get(ArenaEvent, eid)
        team_service.transfer_captain(
            event, db.session.get(ArenaTeam, tid), uids[i + 1]
        )

    res = _parallel(app, 5, transfer)
    assert res.count("ok") >= 1
    with app.app_context():
        assert integrity.check_teams() == []

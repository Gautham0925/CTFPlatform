"""Phase 4: Jeopardy engine — management, solving, isolation, concurrency."""

import datetime
import io
import threading

import pytest

from CTFd.models import ChallengeFiles, Challenges, Users, db
from CTFd.plugins.arena import integrity
from CTFd.plugins.arena.models import (
    ArenaEvent,
    ArenaEventChallenge,
    ArenaHintUnlock,
    ArenaSolve,
)
from CTFd.plugins.arena.services import entity as entity_service
from CTFd.plugins.arena.services import submissions as sub_service
from CTFd.plugins.challenges import CHALLENGE_CLASSES, BaseChallenge, ChallengeResponse
from tests.arena.helpers import (
    create_arena_ctfd,
    gen_event,
    gen_registration,
    gen_role,
    make_user,
)
from tests.helpers import destroy_ctfd, login_as_user

API = "/api/v1/arena"
NOW = datetime.datetime.utcnow()


class ReverseChallenge(BaseChallenge):
    """Custom challenge type: answer is the challenge name reversed."""

    id = name = "reverse"
    challenge_model = Challenges

    @classmethod
    def attempt(cls, challenge, request):
        data = request.form or request.get_json()
        ok = data["submission"].strip() == challenge.name[::-1]
        return ChallengeResponse(
            status="correct" if ok else "incorrect", message="checked"
        )


@pytest.fixture
def app():
    CHALLENGE_CLASSES["reverse"] = ReverseChallenge
    app = create_arena_ctfd()
    app.config["RATELIMIT_LOGIN"] = (1000, 5)
    yield app
    destroy_ctfd(app)
    CHALLENGE_CLASSES.pop("reverse", None)


@pytest.fixture
def admin(app):
    return login_as_user(app, name="admin")


def mk_event(app, slug="e", **kw):
    with app.app_context():
        return gen_event(slug, **kw).id


def player(app, name, *eids):
    with app.app_context():
        uid = make_user(name)
        for e in eids:
            gen_registration(e, uid)
    return uid, login_as_user(app, name=name)


def mk_chal(admin, eid, **kw):
    data = {
        "name": "Baby Web",
        "category": "Web",
        "description": "Find the **flag** <img src=x onerror=alert(1)>",
        "points": 100,
        "visible": True,
        "difficulty": "easy",
        "flags": [{"type": "static", "content": "flag{abc}"}],
        "hints": [
            {"title": "free", "content": "look", "cost": 0},
            {"title": "paid", "content": "secret hint", "cost": 30},
        ],
        "tags": ["web", "beginner"],
    }
    data.update(kw)
    r = admin.post(f"{API}/admin/events/{eid}/challenges", json=data)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["data"]


def submit(c, eid, ecid, flag):
    return c.post(
        f"{API}/events/{eid}/challenges/{ecid}/attempt", json={"submission": flag}
    )


# ------------------------------------------------------------------ management


def test_create_is_sealed_and_native_compatible(app, admin):
    eid = mk_event(app)
    ch = mk_chal(admin, eid)
    assert ch["flags"][0]["content"] == "flag{abc}" and len(ch["hints"]) == 2
    with app.app_context():
        c = db.session.get(Challenges, ch["challenge_id"])
        e = db.session.get(ArenaEvent, eid)
        assert (
            c.module_id == e.sealed_module_id
            and c.state == "hidden"
            and c.type == "standard"
        )
        assert integrity.check_event_challenges() == []


@pytest.mark.parametrize(
    "bad,code",
    [
        ({"type": "nope"}, "invalid:type"),
        ({"flags": [{"type": "regex", "content": "("}]}, "invalid:content"),
        ({"flags": [{"type": "magic", "content": "x"}]}, "invalid:type"),
        ({"scoring": "dynamic"}, "invalid:decay"),
        ({"type_data": {"module_id": 1}}, "invalid:type_data"),
        ({"type_data": {"state": "visible"}}, "invalid:type_data"),
        ({"prerequisites": [99999]}, "invalid:prerequisites"),
        ({"points": -5}, "invalid:points"),
        ({"difficulty": "trivial"}, "invalid:difficulty"),
    ],
)
def test_create_validation(app, admin, bad, code):
    eid = mk_event(app)
    data = {"name": "x", "points": 10}
    data.update(bad)
    r = admin.post(f"{API}/admin/events/{eid}/challenges", json=data)
    assert r.status_code == 400 and r.get_json()["code"] == code, r.get_json()


def test_update_keeps_seal_and_rejects_type_change(app, admin):
    eid = mk_event(app)
    ch = mk_chal(admin, eid)
    r = admin.patch(
        f"{API}/admin/events/{eid}/challenges/{ch['id']}",
        json={"name": "Renamed", "points": 250, "position": 3},
    )
    assert r.status_code == 200 and r.get_json()["data"]["name"] == "Renamed"
    assert (
        admin.patch(
            f"{API}/admin/events/{eid}/challenges/{ch['id']}", json={"type": "dynamic"}
        ).status_code
        == 409
    )
    with app.app_context():
        c = db.session.get(Challenges, ch["challenge_id"])
        assert c.state == "hidden" and c.value == 250
        assert integrity.check_event_challenges() == []


def test_role_permissions_on_challenge_admin(app):
    eid = mk_event(app)
    with app.app_context():
        for name, role in (
            ("cm", "challenge_manager"),
            ("so", "scoreboard_operator"),
            ("an", "analyst"),
        ):
            gen_role(make_user(name), role, eid)
    cm = login_as_user(app, name="cm")
    ch = mk_chal(cm, eid)
    so = login_as_user(app, name="so")
    assert (
        so.post(
            f"{API}/admin/events/{eid}/challenges", json={"name": "x", "points": 1}
        ).status_code
        == 403
    )
    an = login_as_user(app, name="an")
    got = an.get(f"{API}/admin/events/{eid}/challenges/{ch['id']}").get_json()["data"]
    assert "flags" not in got  # analysts never see flags
    assert (
        an.patch(
            f"{API}/admin/events/{eid}/challenges/{ch['id']}", json={"name": "y"}
        ).status_code
        == 403
    )


# --------------------------------------------------------------- participants


def test_listing_states_and_filters(app, admin):
    eid = mk_event(app)
    a = mk_chal(admin, eid, name="Alpha", category="Web")
    mk_chal(admin, eid, name="Hidden", visible=False)
    mk_chal(
        admin,
        eid,
        name="Later",
        available_from=(NOW + datetime.timedelta(hours=1)).isoformat() + "Z",
    )
    mk_chal(admin, eid, name="Gated", category="Cryptography", prerequisites=[a["id"]])
    mk_chal(
        admin,
        eid,
        name="Over",
        available_until=(NOW - datetime.timedelta(minutes=1)).isoformat() + "Z",
    )
    _, c = player(app, "p", eid)
    data = c.get(f"{API}/events/{eid}/challenges").get_json()
    names = {i.get("name"): i["state"] for i in data["data"]}
    assert names == {"Alpha": "open", None: "locked", "Over": "closed"}
    locked = [i for i in data["data"] if i["state"] == "locked"][0]
    assert set(locked) == {"id", "state", "category", "position"}  # no content leak
    assert (
        c.get(f"{API}/events/{eid}/challenges?category=Web").get_json()["data"][0][
            "name"
        ]
        == "Alpha"
    )
    assert (
        c.get(f"{API}/events/{eid}/challenges?q=alp").get_json()["data"][0]["name"]
        == "Alpha"
    )
    assert c.get(f"{API}/events/{eid}/challenges/{locked['id']}").status_code == 403
    assert (
        submit(c, eid, a["id"], "flag{abc}").get_json()["data"]["status"] == "correct"
    )
    names = {
        i.get("name"): i["state"]
        for i in c.get(f"{API}/events/{eid}/challenges").get_json()["data"]
    }
    assert names["Gated"] == "open"  # unlocked by prerequisite


def test_detail_hints_sanitized_and_paid_hidden(app, admin):
    eid = mk_event(app)
    ch = mk_chal(admin, eid)
    _, c = player(app, "p", eid)
    d = c.get(f"{API}/events/{eid}/challenges/{ch['id']}").get_json()["data"]
    assert "onerror" not in d["description_html"]
    free, paid = d["hints"]
    assert free["unlocked"] and "content_html" in free
    assert not paid["unlocked"] and "content_html" not in paid
    assert "flags" not in d


def test_not_started_and_paused(app, admin):
    eid = mk_event(app, status="upcoming", starts_at=NOW + datetime.timedelta(hours=1))
    ch = mk_chal(admin, eid)
    _, c = player(app, "p", eid)
    assert c.get(f"{API}/events/{eid}/challenges").get_json()["code"] == "not_started"
    with app.app_context():
        e = db.session.get(ArenaEvent, eid)
        e.status, e.starts_at = "paused", NOW - datetime.timedelta(hours=1)
        db.session.commit()
    assert c.get(f"{API}/events/{eid}/challenges").status_code == 200
    assert (
        submit(c, eid, ch["id"], "flag{abc}").get_json()["code"] == "event_not_running"
    )


def test_solve_flow_flags_and_attempt_limits(app, admin):
    eid = mk_event(app)
    ch = mk_chal(
        admin,
        eid,
        max_attempts=2,
        flags=[
            {"type": "static", "content": "flag{CaSe}", "case_insensitive": True},
            {"type": "regex", "content": r"^flag\{re[0-9]+\}$"},
        ],
    )
    _, c = player(app, "p", eid)
    r = submit(c, eid, ch["id"], "wrong").get_json()["data"]
    assert r["status"] == "incorrect" and r["attempts_remaining"] == 1
    assert (
        submit(c, eid, ch["id"], "flag{case}").get_json()["data"]["status"] == "correct"
    )
    assert (
        submit(c, eid, ch["id"], "flag{case}").get_json()["data"]["status"]
        == "already_solved"
    )
    _, c2 = player(app, "q", eid)
    assert (
        submit(c2, eid, ch["id"], "flag{re42}").get_json()["data"]["status"]
        == "correct"
    )
    _, c3 = player(app, "r", eid)
    submit(c3, eid, ch["id"], "x")
    submit(c3, eid, ch["id"], "y")
    assert submit(c3, eid, ch["id"], "flag{re1}").get_json()["code"] == "no_attempts"
    with app.app_context():
        assert ArenaSolve.query.filter_by(event_id=eid).count() == 2


def test_rate_limit_per_entity(app, admin):
    eid = mk_event(app, settings={"submissions_per_minute": 3})
    ch = mk_chal(admin, eid)
    _, c = player(app, "p", eid)
    for _ in range(3):
        submit(c, eid, ch["id"], "nope")
    r = submit(c, eid, ch["id"], "flag{abc}")
    assert r.status_code == 429 and r.get_json()["code"] == "ratelimited"


def test_submission_storage_never_keeps_correct_flags(app, admin):
    from CTFd.plugins.arena.models import ArenaSubmission

    eid = mk_event(app)
    ch = mk_chal(admin, eid)
    _, c = player(app, "p", eid)
    submit(c, eid, ch["id"], "bad-guess")
    submit(c, eid, ch["id"], "flag{abc}")
    with app.app_context():
        rows = {s.status: s for s in ArenaSubmission.query.all()}
        assert rows["incorrect"].provided_preview == "bad-guess"
        assert rows["correct"].provided_preview is None
        assert "flag{abc}" not in rows["correct"].provided_hash


def test_dynamic_scoring_is_event_scoped(app, admin):
    e1, e2 = mk_event(app, "e1"), mk_event(app, "e2")
    ch = mk_chal(
        admin,
        e1,
        scoring="dynamic",
        points=500,
        minimum_points=100,
        decay=2,
        decay_function="linear",
    )
    for i in range(3):
        _, c = player(app, f"d{i}", e1)
        submit(c, e1, ch["id"], "flag{abc}")
    got = admin.get(f"{API}/admin/events/{e1}/challenges/{ch['id']}").get_json()["data"]
    assert got["solve_count"] == 3 and got["current_value"] == 500 - 2 * 2
    other = mk_chal(
        admin,
        e2,
        scoring="dynamic",
        points=500,
        minimum_points=100,
        decay=2,
        decay_function="linear",
    )
    assert (
        admin.get(f"{API}/admin/events/{e2}/challenges/{other['id']}").get_json()[
            "data"
        ]["current_value"]
        == 500
    )


def test_per_entity_flags(app, admin):
    eid = mk_event(app)
    ch = mk_chal(admin, eid, per_entity_flag=True, flags=[])
    u1, c1 = player(app, "p1", eid)
    u2, c2 = player(app, "p2", eid)
    with app.app_context():
        e = db.session.get(ArenaEvent, eid)
        ec = db.session.get(ArenaEventChallenge, ch["id"])
        f1 = sub_service.per_entity_flag(e, ec, entity_service.user_key(u1))
        f2 = sub_service.per_entity_flag(e, ec, entity_service.user_key(u2))
    assert f1 != f2
    assert (
        submit(c2, eid, ch["id"], f1).get_json()["data"]["status"] == "incorrect"
    )  # sharing fails
    assert submit(c1, eid, ch["id"], f1).get_json()["data"]["status"] == "correct"


def test_custom_challenge_type(app, admin):
    eid = mk_event(app)
    ch = mk_chal(admin, eid, type="reverse", name="olleh", flags=[])
    _, c = player(app, "p", eid)
    assert submit(c, eid, ch["id"], "nope").get_json()["data"]["status"] == "incorrect"
    assert submit(c, eid, ch["id"], "hello").get_json()["data"]["status"] == "correct"


def test_team_solves_shared(app, admin):
    with app.app_context():
        eid = gen_event("t", team_mode="team", team_size_max=4).id
    ch = mk_chal(admin, eid)
    _, ca = player(app, "a", eid)
    _, cb = player(app, "b", eid)
    _, cn = player(app, "n", eid)
    code = ca.post(f"{API}/events/{eid}/teams", json={"name": "Team"}).get_json()[
        "data"
    ]["join_code"]
    cb.post(f"{API}/events/{eid}/teams/join", json={"join_code": code})
    assert submit(cn, eid, ch["id"], "flag{abc}").get_json()["code"] == "no_team"
    assert (
        submit(ca, eid, ch["id"], "flag{abc}").get_json()["data"]["status"] == "correct"
    )
    assert (
        submit(cb, eid, ch["id"], "flag{abc}").get_json()["data"]["status"]
        == "already_solved"
    )
    lst = cb.get(f"{API}/events/{eid}/challenges").get_json()["data"]
    assert lst[0]["solved"] is True


def test_hint_unlock_once_and_isolated(app, admin):
    e1, e2 = mk_event(app, "e1"), mk_event(app, "e2")
    ch = mk_chal(admin, e1)
    other = mk_chal(admin, e2)
    paid = ch["hints"][1]["id"]
    _, c = player(app, "p", e1)
    r = c.post(f"{API}/events/{e1}/hints/{paid}/unlock", json={})
    assert (
        r.status_code == 200 and "secret hint" in r.get_json()["data"]["content_html"]
    )
    assert c.post(f"{API}/events/{e1}/hints/{paid}/unlock", json={}).status_code == 200
    with app.app_context():
        assert ArenaHintUnlock.query.count() == 1  # cost recorded once
    # hint of an event-2 challenge via event 1
    assert (
        c.post(
            f"{API}/events/{e1}/hints/{other['hints'][1]['id']}/unlock", json={}
        ).status_code
        == 404
    )


def test_file_gateway_and_isolation(app, admin):
    e1, e2 = mk_event(app, "e1"), mk_event(app, "e2")
    ch = mk_chal(admin, e1)
    ch2 = mk_chal(admin, e2)
    r = admin.post(
        f"{API}/admin/events/{e1}/challenges/{ch['id']}/files",
        data={
            "file": (io.BytesIO(b"secret-bytes"), "notes.txt"),
            "nonce": _nonce(admin),
        },
        content_type="multipart/form-data",
    )
    assert r.status_code == 201, r.get_json()
    fid = r.get_json()["data"]["id"]
    _, c = player(app, "p", e1)
    _, c2 = player(app, "q", e2)
    detail = c.get(f"{API}/events/{e1}/challenges/{ch['id']}").get_json()["data"]
    url = detail["files"][0]["url"]
    d = c.get(url)
    assert d.status_code == 200 and d.data == b"secret-bytes"
    assert c2.get(url).status_code == 403  # not a participant of e1
    assert (
        c2.get(f"{API}/events/{e2}/files/{fid}").status_code == 404
    )  # wrong event scope
    with app.app_context():
        location = db.session.get(ChallengeFiles, fid).location
    assert c.get(f"/files/{location}").status_code in (403, 404)  # native route sealed
    assert ch2  # second event exists


def _nonce(client):
    with client.session_transaction() as s:
        return s.get("nonce")


def test_native_endpoints_cannot_reach_event_challenges(app, admin):
    eid = mk_event(app)
    ch = mk_chal(admin, eid)
    _, c = player(app, "p", eid)
    cid = ch["challenge_id"]
    hint_id = ch["hints"][1]["id"]
    assert c.get("/api/v1/challenges").get_json()["data"] == []
    assert c.get(f"/api/v1/challenges/{cid}").status_code == 404
    r = c.post(
        "/api/v1/challenges/attempt",
        json={"challenge_id": cid, "submission": "flag{abc}"},
    )
    assert r.status_code in (403, 404)
    assert c.get(f"/api/v1/hints/{hint_id}").status_code in (403, 404)
    assert c.post(
        "/api/v1/unlocks", json={"target": hint_id, "type": "hints"}
    ).status_code in (400, 403, 404)
    assert c.get(f"/api/v1/challenges/{cid}/solves").status_code in (403, 404)
    with app.app_context():
        from CTFd.models import Solves

        assert Solves.query.count() == 0


def test_cross_event_challenge_access(app, admin):
    e1, e2 = mk_event(app, "e1"), mk_event(app, "e2")
    mk_chal(admin, e1)
    ch2 = mk_chal(admin, e2)
    _, c = player(app, "p", e1)
    assert c.get(f"{API}/events/{e1}/challenges/{ch2['id']}").status_code == 404
    assert submit(c, e1, ch2["id"], "flag{abc}").status_code == 404
    assert c.get(f"{API}/events/{e2}/challenges").status_code == 403  # not registered


def test_delete_and_clone_rules(app, admin):
    e1, e2 = mk_event(app, "e1"), mk_event(app, "e2")
    ch = mk_chal(admin, e1)
    _, c = player(app, "p", e1)
    submit(c, e1, ch["id"], "flag{abc}")
    assert (
        admin.delete(
            f"{API}/admin/events/{e1}/challenges/{ch['id']}", json={}
        ).status_code
        == 409
    )
    with app.app_context():
        org = make_user("org")
        gen_role(org, "organizer", e1)
    org_c = login_as_user(app, name="org")
    assert (
        org_c.post(
            f"{API}/admin/events/{e1}/challenges/{ch['id']}/clone",
            json={"target_event_id": e2},
        ).status_code
        == 404
    )  # no rights on e2
    r = admin.post(
        f"{API}/admin/events/{e1}/challenges/{ch['id']}/clone",
        json={"target_event_id": e2},
    )
    assert r.status_code == 201
    new = r.get_json()["data"]
    assert new["event_id"] == e2 and new["visible"] is False and new["solve_count"] == 0
    with app.app_context():
        assert integrity.check_event_challenges() == []


def _server(app):
    return not app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite")


def test_concurrent_correct_submissions_award_once(app, admin):
    if not _server(app):
        pytest.skip("needs MariaDB/MySQL")
    with app.app_context():
        eid = gen_event("race", team_mode="team", team_size_max=30).id
    ch = mk_chal(admin, eid, scoring="dynamic", points=500, minimum_points=50, decay=10)
    with app.app_context():
        uids = [make_user(f"r{i}") for i in range(30)]
        for u in uids:
            gen_registration(eid, u)
        from CTFd.plugins.arena.services import teams as ts

        event = db.session.get(ArenaEvent, eid)
        _t, code = ts.create_team(
            event, db.session.get(Users, uids[0]), {"name": "Racers"}
        )
        for u in uids[1:]:
            ts.join_with_code(event, db.session.get(Users, u), code)
        others = [make_user(f"o{i}") for i in range(10)]
        for u in others:
            gen_registration(eid, u)
            ts.create_team(event, db.session.get(Users, u), {"name": f"Solo{u}"})

    barrier = threading.Barrier(40, timeout=30)
    results, lock = [], threading.Lock()

    def go(uid):
        barrier.wait()
        with app.app_context():
            try:
                e = db.session.get(ArenaEvent, eid)
                user = db.session.get(Users, uid)
                ent = entity_service.resolve(e, user)
                ec = db.session.get(ArenaEventChallenge, ch["id"])
                r = sub_service.attempt(e, ec, ent, user, "flag{abc}")["status"]
            except Exception as ex:  # noqa: BLE001
                r = getattr(ex, "code", None) or type(ex).__name__
            finally:
                db.session.remove()
            with lock:
                results.append(r)

    ts_ = [threading.Thread(target=go, args=(u,)) for u in uids + others]
    [t.start() for t in ts_]
    [t.join(timeout=120) for t in ts_]
    assert not any(t.is_alive() for t in ts_)
    # Team "Racers": exactly one correct across 30 members; 10 solo teams: 1 each.
    assert results.count("correct") == 11, results
    with app.app_context():
        assert ArenaSolve.query.filter_by(event_id=eid).count() == 11
        ec = db.session.get(ArenaEventChallenge, ch["id"])
        assert ec.solve_count == 11
        assert integrity.check_event_challenges() == []


def test_unsealed_event_is_sealed_before_any_challenge_write(app, admin):
    """Regression: an event without a sealed module must not yield unsealed
    challenges (native /files does not check challenge state)."""
    with app.app_context():
        eid = gen_event("raw", with_sealed_module=False).id
        assert db.session.get(ArenaEvent, eid).sealed_module_id is None
    ch = mk_chal(admin, eid)
    with app.app_context():
        e = db.session.get(ArenaEvent, eid)
        c = db.session.get(Challenges, ch["challenge_id"])
        assert e.sealed_module_id is not None and c.module_id == e.sealed_module_id
        assert integrity.check_event_challenges() == []


def test_integrity_flags_unsealed_challenge(app, admin):
    eid = mk_event(app)
    ch = mk_chal(admin, eid)
    with app.app_context():
        db.session.get(Challenges, ch["challenge_id"]).module_id = None
        db.session.commit()
        problems = integrity.check_event_challenges()
        assert any("NOT SEALED" in p for p in problems)

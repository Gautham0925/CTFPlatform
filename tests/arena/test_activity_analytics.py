"""Phase 7: activity tracking and analytics."""

import datetime
import os

import pytest

from CTFd.config import TestingConfig
from CTFd.models import db
from CTFd.plugins.arena import arena_cache
from CTFd.plugins.arena.models import (
    ArenaActivity,
    ArenaEvent,
    ArenaEventChallenge,
    ArenaSolve,
    ArenaSubmission,
)
from CTFd.plugins.arena.services import activity, analytics, jobs
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
    arena_cache.reset_breaker()
    app = create_arena_ctfd()
    app.config["RATELIMIT_LOGIN"] = (1000, 5)
    yield app
    destroy_ctfd(app)


def setup_event(app, **kw):
    with app.app_context():
        eid = gen_event("e", **kw).id
    admin = login_as_user(app, name="admin")
    ch = admin.post(
        f"{API}/admin/events/{eid}/challenges",
        json={
            "name": "c1",
            "points": 100,
            "visible": True,
            "flags": [{"content": "flag{1}"}],
            "hints": [{"content": "h", "cost": 10}],
        },
    ).get_json()["data"]
    return eid, ch, admin


def player(app, name, eid):
    with app.app_context():
        uid = make_user(name)
        gen_registration(eid, uid)
    return uid, login_as_user(app, name=name)


def actions(app, **filters):
    with app.app_context():
        return [
            a.action
            for a in ArenaActivity.query.filter_by(**filters).order_by(ArenaActivity.id)
        ]


def test_activity_recorded_for_participant_actions(app):
    eid, ch, admin = setup_event(app, registration_open=True)
    with app.app_context():
        uid = make_user("p")
    c = login_as_user(app, name="p")
    c.post(f"{API}/events/{eid}/register", json={})
    c.get(f"{API}/events/{eid}/challenges/{ch['id']}")
    c.post(
        f"{API}/events/{eid}/challenges/{ch['id']}/attempt", json={"submission": "nope"}
    )
    c.post(
        f"{API}/events/{eid}/challenges/{ch['id']}/attempt",
        json={"submission": "flag{1}"},
    )
    hint = ch["hints"][0]["id"]
    c.post(f"{API}/events/{eid}/hints/{hint}/unlock", json={})
    c.get("/logout")
    got = actions(app, actor_user_id=uid)
    assert got == [
        "auth.login",
        "registration.register",
        "challenge.view",
        "challenge.attempt",
        "challenge.solve",
        "hint.unlock",
        "auth.logout",
    ]


def test_activity_never_stores_flags_ips_or_agents(app):
    eid, ch, _admin = setup_event(app)
    uid, c = player(app, "p", eid)
    c.post(
        f"{API}/events/{eid}/challenges/{ch['id']}/attempt",
        json={"submission": "guess-SECRET"},
        headers={"User-Agent": "UA-MARKER"},
    )
    with app.app_context():
        blob = repr([(a.meta, a.target_id) for a in ArenaActivity.query.all()])
    assert (
        "guess-SECRET" not in blob
        and "UA-MARKER" not in blob
        and "127.0.0.1" not in blob
    )
    assert {c.name for c in ArenaActivity.__table__.columns}.isdisjoint(
        {"ip", "user_agent"}
    )


def test_failed_login_not_recorded_as_login(app):
    with app.app_context():
        make_user("p")
    c = app.test_client()
    c.get("/login")
    with c.session_transaction() as s:
        nonce = s["nonce"]
    c.post("/login", data={"name": "p", "password": "wrong", "nonce": nonce})
    assert actions(app, action="auth.login") == []


def test_participant_activity_isolation(app):
    eid, ch, _admin = setup_event(app, team_mode="team", team_size_max=3)
    with app.app_context():
        other = gen_event("other").id
    a, ca = player(app, "a", eid)
    b, cb = player(app, "b", eid)
    x, cx = player(app, "x", eid)
    code = ca.post(f"{API}/events/{eid}/teams", json={"name": "Red"}).get_json()[
        "data"
    ]["join_code"]
    cb.post(f"{API}/events/{eid}/teams/join", json={"join_code": code})
    cx.post(f"{API}/events/{eid}/teams", json={"name": "Blue"})
    cx.get(f"{API}/events/{eid}/challenges/{ch['id']}")
    mine = cb.get(f"{API}/events/{eid}/my-activity").get_json()["data"]
    assert mine and all("actor_user_id" not in r for r in mine)
    assert {r["action"] for r in mine} == {"team.join"}
    team = cb.get(f"{API}/events/{eid}/my-team/activity").get_json()["data"]
    assert {r["actor_user_id"] for r in team} <= {a, b}  # never x's actions
    assert cb.get(f"{API}/admin/events/{eid}/activity").status_code == 403
    assert cb.get(f"{API}/events/{other}/my-activity").status_code == 403


def test_analytics_rbac(app):
    eid, _ch, _admin = setup_event(app)
    with app.app_context():
        gen_role(make_user("an"), "analyst", eid)
        gen_role(make_user("cm"), "challenge_manager", eid)
    an = login_as_user(app, name="an")
    cm = login_as_user(app, name="cm")
    assert an.get(f"{API}/admin/events/{eid}/analytics/overview").status_code == 200
    assert an.get(f"{API}/admin/events/{eid}/export/scoreboard.csv").status_code == 200
    assert cm.get(f"{API}/admin/events/{eid}/analytics/overview").status_code == 403
    assert (
        cm.get(f"{API}/admin/events/{eid}/export/registrations.csv").status_code == 403
    )


def test_rollups_match_source_data(app):
    eid, ch, admin = setup_event(app)
    ch2 = admin.post(
        f"{API}/admin/events/{eid}/challenges",
        json={
            "name": "c2",
            "points": 50,
            "visible": True,
            "flags": [{"content": "flag{2}"}],
        },
    ).get_json()["data"]
    for i in range(4):
        _, c = player(app, f"p{i}", eid)
        c.post(
            f"{API}/events/{eid}/challenges/{ch['id']}/attempt",
            json={"submission": "x"},
        )
        c.post(
            f"{API}/events/{eid}/challenges/{ch['id']}/attempt",
            json={"submission": "flag{1}"},
        )
        if i % 2:
            c.post(
                f"{API}/events/{eid}/challenges/{ch2['id']}/attempt",
                json={"submission": "flag{2}"},
            )
    with app.app_context():
        job = jobs.enqueue("analytics.rollup", {"event_id": eid})
        j = jobs.claim("t")
        assert jobs.run_job(j, "t") == "done" and j.id == job.id
        subs = ArenaSubmission.query.filter_by(event_id=eid).count()
        solves = ArenaSolve.query.filter_by(event_id=eid).count()
        ov = analytics.overview(db.session.get(ArenaEvent, eid))
        assert ov["submissions"] == subs == 10
        assert ov["correct_submissions"] == solves == 6
        assert ov["failed_submissions"] == 4
        assert ov["registrations"]["total"] == 4
        assert ov["completion"]["entities_with_solves"] == 4
        assert ov["completion"]["entities_completed_all"] == 2
        report = {
            r["name"]: r
            for r in analytics.challenge_report(db.session.get(ArenaEvent, eid))
        }
        assert (
            report["c1"]["solves"]
            == db.session.get(ArenaEventChallenge, ch["id"]).solve_count
            == 4
        )
        assert report["c1"]["attempts"] == 8 and report["c1"]["incorrect"] == 4
        assert report["c2"]["solves"] == 2
    tl = admin.get(f"{API}/admin/events/{eid}/analytics/timeline").get_json()["data"]
    assert sum(b["submissions"] for b in tl) == 10
    # Running the rollup again is idempotent.
    with app.app_context():
        jobs.enqueue("analytics.rollup", {"event_id": eid})
        jobs.run_job(jobs.claim("t"), "t")
        assert analytics.overview(db.session.get(ArenaEvent, eid))["submissions"] == 10


def test_csv_exports_and_injection_safety(app):
    eid, ch, admin = setup_event(app, team_mode="team", team_size_max=2)
    _, c = player(app, "p", eid)
    c.post(f"{API}/events/{eid}/teams", json={"name": '=HYPERLINK("http://x")'})
    r = admin.get(f"{API}/admin/events/{eid}/export/teams.csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    body = r.get_data(as_text=True).splitlines()
    assert body[0].startswith("id,name")
    assert "'=HYPERLINK" in body[1]
    assert (
        admin.get(f"{API}/admin/events/{eid}/export/passwords.csv").status_code == 404
    )
    for kind in analytics.EXPORT_KINDS:
        assert (
            admin.get(f"{API}/admin/events/{eid}/export/{kind}.csv").status_code == 200
        ), kind


def test_activity_keyset_pagination_is_bounded(app):
    eid, _ch, admin = setup_event(app)
    with app.app_context():
        db.session.execute(
            ArenaActivity.__table__.insert(),
            [
                {
                    "event_id": eid,
                    "actor_type": "user",
                    "action": "event.view",
                    "created_at": datetime.datetime.utcnow(),
                }
                for _ in range(450)
            ],
        )
        db.session.commit()
    over = admin.get(f"{API}/admin/events/{eid}/activity", query_string={"limit": 1000})
    assert over.status_code == 400  # requests above the cap are rejected
    seen, before = 0, None
    while True:
        q = {"limit": 200}
        if before:
            q["before_id"] = before
        r = admin.get(f"{API}/admin/events/{eid}/activity", query_string=q).get_json()
        assert len(r["data"]) <= 200  # hard cap regardless of requested limit
        seen += len(r["data"])
        before = r["meta"]["next_before_id"]
        if not before:
            break
    assert seen >= 450


def test_retention_prune(app):
    with app.app_context():
        old = datetime.datetime.utcnow() - datetime.timedelta(days=400)
        db.session.execute(
            ArenaActivity.__table__.insert(),
            [
                {"actor_type": "user", "action": "event.view", "created_at": old},
                {"actor_type": "admin", "action": "event.update", "created_at": old},
                {
                    "actor_type": "user",
                    "action": "event.view",
                    "created_at": datetime.datetime.utcnow(),
                },
            ],
        )
        db.session.commit()
        assert activity.prune() == 1  # old participant row only
        kinds = sorted(
            (
                a.actor_type,
                a.created_at < datetime.datetime.utcnow() - datetime.timedelta(days=1),
            )
            for a in ArenaActivity.query.all()
        )
        assert kinds == [("admin", True), ("user", False)]


REDIS_URL = os.environ.get("ARENA_TEST_REDIS_URL")


def test_buffered_activity_with_real_redis():
    if not REDIS_URL:
        pytest.skip("set ARENA_TEST_REDIS_URL")

    class Cfg(TestingConfig):
        CACHE_TYPE = "redis"
        CACHE_REDIS_URL = REDIS_URL
        REDIS_URL = REDIS_URL
        ARENA_ACTIVITY_QUEUE_CAP = 5

    import redis

    redis.Redis.from_url(REDIS_URL).flushdb()
    arena_cache._state.update(client=None, url=None, open_until=0.0)
    app = create_arena_ctfd(config=Cfg)
    try:
        with app.app_context():
            for i in range(8):
                assert (
                    activity.record("event.view", user_id=None, meta={"i": i}) is True
                )
            assert arena_cache.llen(activity.QUEUE_KEY) == 5  # capped, oldest dropped
            assert ArenaActivity.query.count() == 0  # nothing written on the hot path
            assert activity.flush() == 5
            assert arena_cache.llen(activity.QUEUE_KEY) == 0
            metas = sorted(a.meta["i"] for a in ArenaActivity.query.all())
            assert metas == [3, 4, 5, 6, 7]
            # Redis down: record() returns quickly and never raises.
            arena_cache._state["client"] = redis.Redis(
                host="10.255.255.1", socket_timeout=0.2, socket_connect_timeout=0.2
            )
            assert activity.record("event.view") is False
    finally:
        destroy_ctfd(app)
        arena_cache._state.update(client=None, url=None, open_until=0.0)

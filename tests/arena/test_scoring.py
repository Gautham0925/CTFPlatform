"""Phase 5: scoring service, scoreboard, caching, failure handling."""

import datetime
import os
import threading
import time

import pytest

from CTFd.config import TestingConfig
from CTFd.models import Users, db
from CTFd.plugins.arena import arena_cache, integrity
from CTFd.plugins.arena.models import (
    ArenaEvent,
    ArenaEventChallenge,
    ArenaScoreEvent,
    ArenaSolve,
)
from CTFd.plugins.arena.services import entity as entity_service
from CTFd.plugins.arena.services import scoreboard as sb
from CTFd.plugins.arena.services import scoring
from CTFd.plugins.arena.services import submissions as sub_service
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
    sb.clear_local()
    arena_cache.reset_breaker()
    app = create_arena_ctfd()
    app.config["RATELIMIT_LOGIN"] = (1000, 5)
    yield app
    destroy_ctfd(app)


def admin_c(app):
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


def chal(admin, eid, name="c", points=100, flag="flag{x}", **kw):
    data = {
        "name": name,
        "points": points,
        "visible": True,
        "category": kw.pop("category", "Web"),
        "flags": [{"content": flag}],
        **kw,
    }
    r = admin.post(f"{API}/admin/events/{eid}/challenges", json=data)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["data"]


def solve(c, eid, ch, flag="flag{x}"):
    r = c.post(
        f"{API}/events/{eid}/challenges/{ch['id']}/attempt", json={"submission": flag}
    )
    assert r.get_json()["data"]["status"] == "correct", r.get_json()


def board(c, eid, **params):
    return c.get(f"{API}/events/{eid}/scoreboard", query_string=params)


def test_one_solve_one_scoring_event_and_totals(app):
    eid = mk_event(app)
    admin = admin_c(app)
    a = chal(admin, eid, "a", 100)
    b = chal(admin, eid, "b", 250, flag="flag{y}")
    u1, c1 = player(app, "p1", eid)
    _, c2 = player(app, "p2", eid)
    solve(c1, eid, a)
    solve(c1, eid, b, "flag{y}")
    solve(c2, eid, a)
    with app.app_context():
        assert (
            ArenaScoreEvent.query.filter_by(event_id=eid, component="solve").count()
            == 3
        )
        assert scoring.verify(db.session.get(ArenaEvent, eid)) == []
    rows = board(c1, eid).get_json()["data"]["standings"]
    assert [(r["name"], r["score"], r["rank"]) for r in rows] == [
        ("p1", 350, 1),
        ("p2", 100, 2),
    ]
    assert rows[0]["breakdown"]["jeopardy"] == 350


def test_dynamic_value_applies_retroactively(app):
    eid = mk_event(app)
    admin = admin_c(app)
    d = chal(
        admin,
        eid,
        "dyn",
        500,
        scoring="dynamic",
        minimum_points=100,
        decay=1,
        decay_function="linear",
    )
    _, c1 = player(app, "p1", eid)
    _, c2 = player(app, "p2", eid)
    solve(c1, eid, d)
    solve(c2, eid, d)
    rows = board(c1, eid).get_json()["data"]["standings"]
    assert {r["score"] for r in rows} == {
        499
    }  # native semantics: current value for all


def test_first_blood_hint_costs_and_adjustments(app):
    eid = mk_event(app, settings={"first_blood_bonus": 50})
    admin = admin_c(app)
    a = chal(admin, eid, "a", 100, hints=[{"content": "h", "cost": 20}])
    u1, c1 = player(app, "p1", eid)
    u2, c2 = player(app, "p2", eid)
    solve(c1, eid, a)
    solve(c2, eid, a)
    hint_id = admin.get(f"{API}/admin/events/{eid}/challenges/{a['id']}").get_json()[
        "data"
    ]["hints"][0]["id"]
    c2.post(f"{API}/events/{eid}/hints/{hint_id}/unlock", json={})
    c2.post(f"{API}/events/{eid}/hints/{hint_id}/unlock", json={})  # idempotent
    r = admin.post(
        f"{API}/admin/events/{eid}/score-adjustments",
        json={
            "entity_key": f"u:{u2}",
            "points": 5,
            "reason": "writeup",
            "idempotency_key": "w1",
        },
    )
    assert r.status_code == 201
    r = admin.post(
        f"{API}/admin/events/{eid}/score-adjustments",
        json={
            "entity_key": f"u:{u2}",
            "points": 5,
            "reason": "writeup",
            "idempotency_key": "w1",
        },
    )
    assert r.status_code == 409  # applied once
    assert (
        admin.post(
            f"{API}/admin/events/{eid}/score-adjustments",
            json={"entity_key": "u:999999", "points": 5, "reason": "x"},
        ).status_code
        == 404
    )
    assert (
        admin.post(
            f"{API}/admin/events/{eid}/score-adjustments",
            json={"entity_key": f"u:{u2}", "points": 0, "reason": "x"},
        ).status_code
        == 400
    )
    scores = {
        r["name"]: r["score"] for r in board(c1, eid).get_json()["data"]["standings"]
    }
    assert scores == {"p1": 150, "p2": 100 - 20 + 5}
    with app.app_context():
        assert (
            ArenaScoreEvent.query.filter_by(
                event_id=eid, component="first_blood"
            ).count()
            == 1
        )


def test_tie_break_is_stable_by_time_reached(app):
    eid = mk_event(app)
    admin = admin_c(app)
    a = chal(admin, eid, "a", 100)
    _, c_late = player(app, "late", eid)
    _, c_early = player(app, "early", eid)
    solve(c_early, eid, a)
    time.sleep(0.01)
    solve(c_late, eid, a)
    rows = board(c_early, eid).get_json()["data"]["standings"]
    assert [r["name"] for r in rows] == ["early", "late"]
    assert [r["rank"] for r in rows] == [1, 2]


def test_freeze_hides_later_solves_from_public(app):
    eid = mk_event(app)
    admin = admin_c(app)
    a = chal(admin, eid, "a", 100)
    _, c1 = player(app, "p1", eid)
    r = admin.post(
        f"{API}/admin/events/{eid}/scoreboard/freeze",
        json={"freeze_at": (datetime.datetime.utcnow()).isoformat() + "Z"},
    )
    assert r.status_code == 200
    time.sleep(0.01)
    solve(c1, eid, a)
    pub = board(c1, eid).get_json()["data"]
    assert pub["frozen"] is True and pub["standings"] == []
    adm = admin.get(f"{API}/admin/events/{eid}/scoreboard").get_json()["data"]
    assert [r["score"] for r in adm["standings"]] == [100]


def test_team_board_individual_view_and_banned_teams(app):
    with app.app_context():
        eid = gen_event(
            "t",
            team_mode="team",
            team_size_max=3,
            settings={"individual_scoring": True},
        ).id
    admin = admin_c(app)
    a = chal(admin, eid, "a", 100)
    b = chal(admin, eid, "b", 50, flag="flag{b}")
    _, ca = player(app, "a", eid)
    _, cb = player(app, "b", eid)
    _, cz = player(app, "z", eid)
    code = ca.post(f"{API}/events/{eid}/teams", json={"name": "Red"}).get_json()[
        "data"
    ]["join_code"]
    cb.post(f"{API}/events/{eid}/teams/join", json={"join_code": code})
    tz = cz.post(f"{API}/events/{eid}/teams", json={"name": "Blue"}).get_json()["data"]
    solve(ca, eid, a)
    solve(cb, eid, b, "flag{b}")
    solve(cz, eid, a)
    teams = {
        r["name"]: r["score"] for r in board(ca, eid).get_json()["data"]["standings"]
    }
    assert teams == {"Red": 150, "Blue": 100}
    ind = {
        r["name"]: r["score"]
        for r in board(ca, eid, view="individuals").get_json()["data"]["standings"]
    }
    assert ind == {"a": 100, "b": 50, "z": 100}
    admin.post(f"{API}/admin/events/{eid}/teams/{tz['id']}/ban", json={})
    teams = {r["name"] for r in board(ca, eid).get_json()["data"]["standings"]}
    assert teams == {"Red"}
    adm = {
        r["name"]
        for r in admin.get(f"{API}/admin/events/{eid}/scoreboard").get_json()["data"][
            "standings"
        ]
    }
    assert adm == {"Red", "Blue"}


def test_visibility_modes(app):
    eid = mk_event(app, settings={"scoreboard_visibility": "participants"})
    _, c = player(app, "p", eid)
    assert board(app.test_client(), eid).status_code == 403
    assert board(c, eid).status_code == 200
    with app.app_context():
        db.session.get(ArenaEvent, eid).settings = {"scoreboard_visibility": "admins"}
        db.session.commit()
    assert board(c, eid).status_code == 403
    assert board(admin_c(app), eid).status_code == 200


def test_etag_conditional_requests(app):
    eid = mk_event(app)
    admin = admin_c(app)
    a = chal(admin, eid, "a")
    _, c = player(app, "p", eid)
    r1 = board(c, eid)
    tag = r1.headers["ETag"]
    r2 = c.get(f"{API}/events/{eid}/scoreboard", headers={"If-None-Match": tag})
    assert r2.status_code == 304 and r2.data == b""
    solve(c, eid, a)
    r3 = c.get(f"{API}/events/{eid}/scoreboard", headers={"If-None-Match": tag})
    assert r3.status_code == 200 and r3.headers["ETag"] != tag
    assert r3.headers["X-Arena-Refresh"] == "15"


def test_pagination(app):
    eid = mk_event(app)
    admin = admin_c(app)
    a = chal(admin, eid, "a")
    for i in range(5):
        _, c = player(app, f"p{i}", eid)
        solve(c, eid, a)
    r = board(c, eid, per_page=2, page=3).get_json()
    assert len(r["data"]["standings"]) == 1 and r["meta"]["pagination"]["total"] == 5
    assert r["data"]["standings"][0]["rank"] == 5


def test_cross_event_scores_are_isolated(app):
    e1, e2 = mk_event(app, "e1"), mk_event(app, "e2")
    admin = admin_c(app)
    a = chal(admin, e1, "a", 100)
    chal(admin, e2, "b", 100)
    _, c = player(app, "p", e1, e2)
    solve(c, e1, a)
    assert len(board(c, e1).get_json()["data"]["standings"]) == 1
    assert board(c, e2).get_json()["data"]["standings"] == []


def test_scoreboard_survives_cache_outage(app, monkeypatch):
    eid = mk_event(app)
    admin = admin_c(app)
    a = chal(admin, eid, "a")
    _, c = player(app, "p", eid)
    solve(c, eid, a)
    healthy = board(c, eid).get_json()["data"]["standings"]
    # Simulate the cache backend failing on every call.
    with app.app_context():

        def broken(*args, **kwargs):
            raise ConnectionError("cache down")

        from CTFd.cache import cache

        monkeypatch.setattr(
            arena_cache,
            "_fallback",
            lambda: type(
                "X", (), {"get": broken, "set": broken, "add": broken, "delete": broken}
            )(),
        )
        monkeypatch.setattr(arena_cache, "_client", lambda: None)
        assert cache is not None
    r = board(c, eid)
    assert r.status_code == 200
    assert r.get_json()["data"]["standings"] == healthy
    assert "ETag" not in r.headers  # no conditional caching while degraded
    # Submissions still work (DB guarantees) while the cache is down.
    b = chal(admin, eid, "b", flag="flag{b}")
    solve(c, eid, b, "flag{b}")
    assert board(c, eid).get_json()["data"]["standings"][0]["score"] == 200


def test_verify_endpoint_and_tamper_detection(app):
    eid = mk_event(app)
    admin = admin_c(app)
    a = chal(admin, eid, "a")
    u, c = player(app, "p", eid)
    solve(c, eid, a)
    v = admin.get(f"{API}/admin/events/{eid}/scoreboard/verify").get_json()["data"]
    assert v["consistent"] is True, v
    with app.app_context():
        db.session.add(
            ArenaScoreEvent(
                event_id=eid,
                entity_key=f"u:{u}",
                component="solve",
                points=100,
                source="solve",
                source_ref="999999",
            )
        )
        db.session.commit()
        assert integrity.check_scoring()
    v = admin.get(f"{API}/admin/events/{eid}/scoreboard/verify").get_json()["data"]
    assert v["consistent"] is False


def test_categories_history_and_stats(app):
    eid = mk_event(app)
    admin = admin_c(app)
    a = chal(admin, eid, "a", 100, category="Web")
    b = chal(admin, eid, "b", 200, flag="flag{b}", category="Cryptography")
    chal(admin, eid, "secret", 999, flag="flag{s}", visible=False)
    _, c = player(app, "p", eid)
    solve(c, eid, a)
    solve(c, eid, b, "flag{b}")
    cats = c.get(f"{API}/events/{eid}/scoreboard/categories").get_json()["data"]
    assert cats["Web"][0]["score"] == 100 and cats["Cryptography"][0]["score"] == 200
    hist = c.get(f"{API}/events/{eid}/scoreboard/history").get_json()["data"]
    assert [p[1] for p in hist[0]["series"]] == [100, 300]
    stats = c.get(f"{API}/events/{eid}/scoreboard/challenges").get_json()["data"]
    assert {s["name"] for s in stats} == {"a", "b"}  # hidden challenge not revealed
    assert c.get(f"{API}/events/{eid}/my-score").get_json()["data"]["score"] == 300


def test_rbac_scoring_endpoints(app):
    eid = mk_event(app)
    from tests.arena.helpers import gen_role

    with app.app_context():
        gen_role(make_user("so"), "scoreboard_operator", eid)
        gen_role(make_user("cm"), "challenge_manager", eid)
        gen_role(make_user("an"), "analyst", eid)
        uid = make_user("target")
        gen_registration(eid, uid)
    body = {"entity_key": f"u:{uid}", "points": 1, "reason": "r"}
    assert (
        login_as_user(app, name="so")
        .post(f"{API}/admin/events/{eid}/score-adjustments", json=body)
        .status_code
        == 201
    )
    assert (
        login_as_user(app, name="cm")
        .post(f"{API}/admin/events/{eid}/score-adjustments", json=body)
        .status_code
        == 403
    )
    an = login_as_user(app, name="an")
    assert (
        an.post(f"{API}/admin/events/{eid}/score-adjustments", json=body).status_code
        == 403
    )
    assert an.get(f"{API}/admin/events/{eid}/score-events").status_code == 200


# ------------------------------------------------------------- real Redis

REDIS_URL = os.environ.get("ARENA_TEST_REDIS_URL")


@pytest.fixture
def redis_app():
    if not REDIS_URL:
        pytest.skip("set ARENA_TEST_REDIS_URL=redis://localhost:6379/15")

    class Cfg(TestingConfig):
        CACHE_TYPE = "redis"
        CACHE_REDIS_URL = REDIS_URL
        REDIS_URL = REDIS_URL

    import redis

    redis.Redis.from_url(REDIS_URL).flushdb()
    arena_cache._state.update(client=None, url=None, open_until=0.0)
    sb.clear_local()
    app = create_arena_ctfd(config=Cfg)
    app.config["RATELIMIT_LOGIN"] = (1000, 5)
    yield app
    destroy_ctfd(app)
    arena_cache._state.update(client=None, url=None, open_until=0.0)


def test_real_redis_etag_and_outage(redis_app):
    app = redis_app
    eid = mk_event(app)
    admin = admin_c(app)
    a = chal(admin, eid, "a")
    _, c = player(app, "p", eid)
    solve(c, eid, a)
    r1 = board(c, eid)
    assert r1.status_code == 200 and r1.headers.get("ETag")
    assert (
        c.get(
            f"{API}/events/{eid}/scoreboard",
            headers={"If-None-Match": r1.headers["ETag"]},
        ).status_code
        == 304
    )
    # Point Arena's client at a black-holed address: requests must stay fast.
    import redis

    arena_cache._state["client"] = redis.Redis(
        host="10.255.255.1", port=6379, socket_timeout=0.25, socket_connect_timeout=0.25
    )
    arena_cache._state["url"] = app.config["CACHE_REDIS_URL"]
    start = time.monotonic()
    r = board(c, eid)
    elapsed = time.monotonic() - start
    assert r.status_code == 200 and r.get_json()["data"]["standings"][0]["score"] == 100
    assert elapsed < 1.5, elapsed
    # Breaker open: subsequent requests don't wait for timeouts at all.
    start = time.monotonic()
    assert board(c, eid).status_code == 200
    assert time.monotonic() - start < 0.5


# ---------------------------------------------------------- concurrency


def test_concurrent_scoring_consistency(app):
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        pytest.skip("needs MariaDB/MySQL")
    eid = mk_event(app, settings={"first_blood_bonus": 25})
    admin = admin_c(app)
    chals = [chal(admin, eid, f"c{i}", 100, flag=f"flag{{{i}}}") for i in range(3)]
    with app.app_context():
        uids = [make_user(f"cc{i}") for i in range(20)]
        for u in uids:
            gen_registration(eid, u)
    barrier = threading.Barrier(len(uids), timeout=30)
    errors = []

    def run(uid):
        barrier.wait()
        with app.app_context():
            try:
                e = db.session.get(ArenaEvent, eid)
                user = db.session.get(Users, uid)
                ent = entity_service.resolve(e, user)
                for i, ch in enumerate(chals):
                    ec = db.session.get(ArenaEventChallenge, ch["id"])
                    sub_service.attempt(e, ec, ent, user, f"flag{{{i}}}")
            except Exception as ex:  # noqa: BLE001
                errors.append(repr(ex))
            finally:
                db.session.remove()

    ts = [threading.Thread(target=run, args=(u,)) for u in uids]
    [t.start() for t in ts]
    [t.join(timeout=120) for t in ts]
    assert not errors, errors
    with app.app_context():
        e = db.session.get(ArenaEvent, eid)
        assert ArenaSolve.query.filter_by(event_id=eid).count() == 60
        assert (
            ArenaScoreEvent.query.filter_by(event_id=eid, component="solve").count()
            == 60
        )
        assert (
            ArenaScoreEvent.query.filter_by(
                event_id=eid, component="first_blood"
            ).count()
            == 3
        )
        assert scoring.verify(e) == []
        rows = scoring.compute_standings(e, scope="admin")
        assert sum(r["score"] for r in rows) == 60 * 100 + 3 * 25


def test_verify_is_independent_of_connection_collation(app):
    """Regression: found restoring a backup into the Profile B stack, where the
    MariaDB connection collation (utf8mb4_general_ci) differed from the
    columns' (utf8mb4_unicode_ci) and a CAST(...)=source_ref join raised 1267."""
    from sqlalchemy import text

    eid = mk_event(app)
    admin = admin_c(app)
    a = chal(admin, eid, "a")
    _, c = player(app, "p", eid)
    solve(c, eid, a)
    with app.app_context():
        if db.engine.dialect.name == "mysql":
            column = db.session.execute(
                text(
                    "SELECT COLLATION_NAME FROM information_schema.COLUMNS WHERE "
                    "TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'arena_score_events' "
                    "AND COLUMN_NAME = 'source_ref'"
                )
            ).scalar()
            other = (
                "utf8mb4_general_ci"
                if column != "utf8mb4_general_ci"
                else "utf8mb4_unicode_ci"
            )
            db.session.execute(text(f"SET collation_connection = '{other}'"))
        e = db.session.get(ArenaEvent, eid)
        assert scoring.verify(e) == []
        assert integrity.check_scoring() == []

"""Phase 9: Attack & Defense."""

import datetime
import json

import pytest

from CTFd.models import Users, db
from CTFd.plugins.arena.execution import client as runner_client
from CTFd.plugins.arena.execution.client import MockRunnerClient
from CTFd.plugins.arena.models import (
    ArenaADCheck,
    ArenaADRound,
    ArenaADTeamService,
    ArenaEvent,
    ArenaScoreEvent,
    ArenaTeam,
)
from CTFd.plugins.arena.services import ad, jobs, scoring
from tests.arena.helpers import (
    create_arena_ctfd,
    gen_event,
    gen_registration,
    gen_role,
    make_user,
)
from tests.helpers import destroy_ctfd, login_as_user

API = "/api/v1/arena"
IMG = "registry.local/svc@sha256:" + "e" * 64


@pytest.fixture
def app():
    runner_client.set_client(MockRunnerClient())
    app = create_arena_ctfd()
    app.config["RATELIMIT_LOGIN"] = (1000, 5)
    yield app
    runner_client.set_client(None)
    destroy_ctfd(app)


def run_jobs(app):
    with app.app_context():
        while (j := jobs.claim("t")) is not None:
            jobs.run_job(j, "t")


def build_game(app, teams=("Alpha", "Bravo", "Charlie"), **settings):
    settings.setdefault("ad_round_seconds", 60)
    with app.app_context():
        eid = gen_event(
            "ad",
            type="ad",
            ad_enabled=True,
            jeopardy_enabled=False,
            team_mode="team",
            team_size_max=2,
            settings=settings,
        ).id
    admin = login_as_user(app, name="admin")
    img = admin.post(
        f"{API}/admin/platform/images",
        json={"name": "svc", "version": "1", "reference": IMG, "ports": [1337]},
    ).get_json()["data"]
    svc = admin.post(
        f"{API}/admin/events/{eid}/ad/services",
        json={
            "name": "notes",
            "image_id": img["id"],
            "attack_points": 10,
            "defense_points": 2,
            "loss_points": 5,
            "sla_points": 1,
        },
    ).get_json()["data"]
    clients, team_ids = {}, {}
    for name in teams:
        with app.app_context():
            uid = make_user(name.lower())
            gen_registration(eid, uid)
        c = login_as_user(app, name=name.lower())
        team_ids[name] = c.post(
            f"{API}/events/{eid}/teams", json={"name": name}
        ).get_json()["data"]["id"]
        clients[name] = c
    assert admin.post(f"{API}/admin/events/{eid}/ad/provision", json={}).get_json()[
        "data"
    ]["created"] == len(teams)
    run_jobs(app)  # instances.provision via MockRunner
    return eid, admin, svc, clients, team_ids


def tick(app, eid, now=None):
    with app.app_context():
        out = ad.tick(db.session.get(ArenaEvent, eid), now=now)
    run_jobs(app)
    return out


def flag_of(app, eid, round_number, team_id, service_id):
    with app.app_context():
        return ad.flag_value(
            db.session.get(ArenaEvent, eid), round_number, team_id, service_id
        )


def end_round(app, eid):
    with app.app_context():
        rnd = ArenaADRound.query.filter_by(event_id=eid, status="active").first()
        rnd.ends_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        db.session.commit()
    return tick(app, eid)


def totals(app, eid):
    with app.app_context():
        rows = scoring.compute_standings(db.session.get(ArenaEvent, eid), scope="admin")
        return {r["name"]: r["breakdown"]["attack_defense"] for r in rows}


def test_provisioning_gives_each_team_an_isolated_service(app):
    eid, _admin, _svc, clients, team_ids = build_game(app)
    with app.app_context():
        rows = ArenaADTeamService.query.filter_by(event_id=eid).all()
        assert sorted(r.team_id for r in rows) == sorted(team_ids.values())
        assert len({r.instance_id for r in rows}) == 3  # one instance per team
    st = clients["Alpha"].get(f"{API}/events/{eid}/ad/status").get_json()["data"]
    assert len(st["my_services"]) == 1 and st["my_services"][0]["endpoint"]["host"]
    assert {t["team_id"] for t in st["targets"]} == {
        team_ids["Bravo"],
        team_ids["Charlie"],
    }
    blob = json.dumps(st)
    assert "FLAG_" not in blob and "uuid" not in blob and "entity_key" not in blob


def test_round_capture_rules_and_scoring(app):
    eid, admin, svc, clients, t = build_game(app)
    assert tick(app, eid).startswith("opened 1")
    with app.app_context():
        assert ArenaADCheck.query.filter_by(event_id=eid).count() == 3  # checker ran
    bravo_flag = flag_of(app, eid, 1, t["Bravo"], svc["id"])
    a = clients["Alpha"]
    r = a.post(
        f"{API}/events/{eid}/ad/flags",
        json={"flags": [bravo_flag, bravo_flag, "FLAG_NOPE", "junk"]},
    )
    assert [x["result"] for x in r.get_json()["data"]["results"]] == [
        "accepted",
        "duplicate",
        "invalid",
        "invalid",
    ]
    own = clients["Bravo"].post(
        f"{API}/events/{eid}/ad/flags", json={"flags": [bravo_flag]}
    )
    assert own.get_json()["data"]["results"][0]["result"] == "own_flag"
    # Charlie can also steal Bravo's flag (once per attacker).
    clients["Charlie"].post(
        f"{API}/events/{eid}/ad/flags", json={"flags": [bravo_flag]}
    )
    end_round(app, eid)
    # Alpha: attack 10 + sla 1 + defended 2 ; Bravo: sla 1 - 5 - 5 ; Charlie: 10 + 1 + 2
    assert totals(app, eid) == {"Alpha": 13, "Bravo": -9, "Charlie": 13}
    with app.app_context():
        r1 = ArenaADRound.query.filter_by(event_id=eid, number=1).one()
        assert r1.status == "closed"
        assert (
            ArenaADRound.query.filter_by(
                event_id=eid, number=2, status="active"
            ).count()
            == 1
        )
        before = ArenaScoreEvent.query.filter_by(event_id=eid).count()
        ad.score_round(r1)  # replay: idempotent
        ad.close_round(r1)
        assert ArenaScoreEvent.query.filter_by(event_id=eid).count() == before


def test_flag_expiry_and_cross_event_flags(app):
    eid, _admin, svc, clients, t = build_game(app, ad_flag_lifetime_rounds=2)
    tick(app, eid)
    old = flag_of(app, eid, 1, t["Bravo"], svc["id"])
    end_round(app, eid)  # -> round 2
    end_round(app, eid)  # -> round 3
    r = clients["Alpha"].post(f"{API}/events/{eid}/ad/flags", json={"flags": [old]})
    assert r.get_json()["data"]["results"][0]["result"] == "expired"
    fresh = flag_of(app, eid, 3, t["Bravo"], svc["id"])
    r = clients["Alpha"].post(f"{API}/events/{eid}/ad/flags", json={"flags": [fresh]})
    assert r.get_json()["data"]["results"][0]["result"] == "accepted"
    with app.app_context():
        other = gen_event("other-ad", type="ad", ad_enabled=True, team_mode="team")
        foreign = ad.flag_value(other, 3, t["Bravo"], svc["id"])
    r = clients["Alpha"].post(f"{API}/events/{eid}/ad/flags", json={"flags": [foreign]})
    assert r.get_json()["data"]["results"][0]["result"] == "invalid"


def test_warmup_rounds_reject_captures(app):
    eid, _admin, svc, clients, t = build_game(app, ad_warmup_rounds=1)
    tick(app, eid)
    f = flag_of(app, eid, 1, t["Bravo"], svc["id"])
    r = clients["Alpha"].post(f"{API}/events/{eid}/ad/flags", json={"flags": [f]})
    assert r.get_json()["data"]["results"][0]["result"] == "warmup"


def test_down_service_gets_no_sla_or_defense(app):
    eid, admin, svc, clients, t = build_game(app)
    with app.app_context():
        ts = ArenaADTeamService.query.filter_by(team_id=t["Charlie"]).one()
        ts_id = ts.id
    assert (
        admin.post(
            f"{API}/admin/events/{eid}/ad/team-services/{ts_id}/mock-status",
            json={"status": "down"},
        ).status_code
        == 200
    )
    tick(app, eid)
    end_round(app, eid)
    assert totals(app, eid) == {"Alpha": 3, "Bravo": 3}  # Charlie scored nothing
    up = {
        u["team_id"]: u["uptime"]
        for u in admin.get(f"{API}/admin/events/{eid}/ad/uptime").get_json()["data"]
    }
    assert up[t["Charlie"]] == 0.0 and up[t["Alpha"]] == 1.0


def test_automatic_round_transitions_and_event_end(app):
    eid, _admin, _svc, _clients, _t = build_game(app)
    tick(app, eid)
    for _ in range(3):
        end_round(app, eid)
    with app.app_context():
        assert [
            r.status
            for r in ArenaADRound.query.filter_by(event_id=eid).order_by(
                ArenaADRound.number
            )
        ] == ["closed", "closed", "closed", "active"]
        e = db.session.get(ArenaEvent, eid)
        e.ends_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        db.session.commit()
    end_round(app, eid)
    with app.app_context():
        assert (
            ArenaADRound.query.filter_by(event_id=eid, status="active").count() == 0
        )  # no new round


def test_flag_rate_limit(app):
    eid, _admin, svc, clients, t = build_game(app, ad_flag_submissions_per_minute=2)
    tick(app, eid)
    a = clients["Alpha"]
    assert (
        a.post(f"{API}/events/{eid}/ad/flags", json={"flags": ["FLAG_A"]}).status_code
        == 200
    )
    assert (
        a.post(f"{API}/events/{eid}/ad/flags", json={"flags": ["FLAG_B"]}).status_code
        == 200
    )
    r = a.post(f"{API}/events/{eid}/ad/flags", json={"flags": ["FLAG_C"]})
    assert r.status_code == 429


def test_access_control(app):
    eid, admin, svc, clients, t = build_game(app)
    tick(app, eid)
    a = clients["Alpha"]
    assert (
        a.post(f"{API}/admin/events/{eid}/ad/rounds/close", json={}).status_code == 403
    )
    assert a.get(f"{API}/admin/events/{eid}/ad/captures").status_code == 403
    with app.app_context():
        gen_role(make_user("op"), "ad_operator", eid)
        gen_role(make_user("an"), "analyst", eid)
        jeop = gen_event("jeop").id
        uid = make_user("solo")
        gen_registration(jeop, uid)
    op = login_as_user(app, name="op")
    assert (
        op.post(f"{API}/admin/events/{eid}/ad/rounds/close", json={}).status_code == 200
    )
    an = login_as_user(app, name="an")
    assert an.get(f"{API}/admin/events/{eid}/ad/checks").status_code == 200
    assert (
        an.post(f"{API}/admin/events/{eid}/ad/rounds/open", json={}).status_code == 403
    )
    solo = login_as_user(app, name="solo")
    assert (
        solo.get(f"{API}/events/{jeop}/ad/status").status_code == 404
    )  # not an A&D event
    assert (
        solo.get(f"{API}/events/{eid}/ad/status").status_code == 403
    )  # not registered


def _simulate(app):
    eid, _admin, svc, clients, t = build_game(app)
    tick(app, eid)
    pattern = [
        ("Alpha", "Bravo"),
        ("Charlie", "Alpha"),
        ("Bravo", "Charlie"),
        ("Alpha", "Charlie"),
    ]
    for rnd in range(1, 9):
        for attacker, victim in pattern[: (rnd % 4) + 1]:
            f = flag_of(app, eid, rnd, t[victim], svc["id"])
            clients[attacker].post(f"{API}/events/{eid}/ad/flags", json={"flags": [f]})
        end_round(app, eid)
    return totals(app, eid)


def test_deterministic_replay_of_a_simulated_game():
    results = []
    for _ in range(2):
        runner_client.set_client(MockRunnerClient())
        app = create_arena_ctfd()
        app.config["RATELIMIT_LOGIN"] = (1000, 5)
        try:
            results.append(_simulate(app))
        finally:
            destroy_ctfd(app)
    assert results[0] == results[1]
    assert sum(results[0].values()) != 0


def test_concurrent_captures_award_once(app):
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        pytest.skip("needs MariaDB/MySQL")
    import threading

    eid, _admin, svc, clients, t = build_game(app)
    tick(app, eid)
    f = flag_of(app, eid, 1, t["Bravo"], svc["id"])
    with app.app_context():
        uid = Users.query.filter_by(name="alpha").one().id
    barrier = threading.Barrier(12, timeout=30)
    out, lock = [], threading.Lock()

    def go():
        barrier.wait()
        with app.app_context():
            try:
                e = db.session.get(ArenaEvent, eid)
                team = db.session.get(ArenaTeam, t["Alpha"])
                r = ad.submit_flags(e, team, db.session.get(Users, uid), [f])[0][
                    "result"
                ]
            finally:
                db.session.remove()
            with lock:
                out.append(r)

    ts = [threading.Thread(target=go) for _ in range(12)]
    [x.start() for x in ts]
    [x.join(timeout=60) for x in ts]
    assert out.count("accepted") == 1 and out.count("duplicate") == 11, out

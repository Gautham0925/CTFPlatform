"""Phase 10: mixed competition scoring."""

import datetime

import pytest

from CTFd.models import db
from CTFd.plugins.arena.execution import client as runner_client
from CTFd.plugins.arena.execution.client import MockRunnerClient
from CTFd.plugins.arena.models import ArenaADRound, ArenaEvent, ArenaScoreSnapshot
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
IMG = "registry.local/svc@sha256:" + "f" * 64


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


def mixed_game(app):
    """Red: one Jeopardy solve (100). Blue: steals Red's A&D flag (attack 10).
    Both services up: SLA 1 each; Blue defended (+2), Red lost (-5)."""
    with app.app_context():
        eid = gen_event(
            "mix",
            type="mixed",
            ad_enabled=True,
            jeopardy_enabled=True,
            team_mode="team",
            team_size_max=2,
            settings={"ad_round_seconds": 60},
        ).id
    admin = login_as_user(app, name="admin")
    ch = admin.post(
        f"{API}/admin/events/{eid}/challenges",
        json={
            "name": "web1",
            "points": 100,
            "visible": True,
            "flags": [{"content": "flag{w}"}],
        },
    ).get_json()["data"]
    img = admin.post(
        f"{API}/admin/platform/images",
        json={"name": "svc", "version": "1", "reference": IMG, "ports": [9000]},
    ).get_json()["data"]
    svc = admin.post(
        f"{API}/admin/events/{eid}/ad/services",
        json={
            "name": "vault",
            "image_id": img["id"],
            "attack_points": 10,
            "defense_points": 2,
            "loss_points": 5,
            "sla_points": 1,
        },
    ).get_json()["data"]
    teams, clients = {}, {}
    for name in ("Red", "Blue"):
        with app.app_context():
            uid = make_user(name.lower())
            gen_registration(eid, uid)
        c = login_as_user(app, name=name.lower())
        teams[name] = c.post(
            f"{API}/events/{eid}/teams", json={"name": name}
        ).get_json()["data"]["id"]
        clients[name] = c
    admin.post(f"{API}/admin/events/{eid}/ad/provision", json={})
    run_jobs(app)
    with app.app_context():
        ad.tick(db.session.get(ArenaEvent, eid))
    run_jobs(app)
    clients["Red"].post(
        f"{API}/events/{eid}/challenges/{ch['id']}/attempt",
        json={"submission": "flag{w}"},
    )
    with app.app_context():
        flag = ad.flag_value(
            db.session.get(ArenaEvent, eid), 1, teams["Red"], svc["id"]
        )
    clients["Blue"].post(f"{API}/events/{eid}/ad/flags", json={"flags": [flag]})
    with app.app_context():
        rnd = ArenaADRound.query.filter_by(event_id=eid, status="active").one()
        rnd.ends_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        db.session.commit()
        ad.tick(db.session.get(ArenaEvent, eid))
    return eid, admin, clients


def board(app, eid):
    with app.app_context():
        return {
            r["name"]: r
            for r in scoring.compute_standings(
                db.session.get(ArenaEvent, eid), scope="admin"
            )
        }


def test_unified_board_combines_both_modes(app):
    eid, _admin, clients = mixed_game(app)
    b = board(app, eid)
    assert (
        b["Red"]["breakdown"]["jeopardy"] == 100
        and b["Red"]["breakdown"]["attack_defense"] == 1 - 5
    )
    assert (
        b["Blue"]["breakdown"]["jeopardy"] == 0
        and b["Blue"]["breakdown"]["attack_defense"] == 10 + 1 + 2
    )
    assert b["Red"]["score"] == 96 and b["Blue"]["score"] == 13
    pub = (
        clients["Red"]
        .get(f"{API}/events/{eid}/scoreboard")
        .get_json()["data"]["standings"]
    )
    assert [r["name"] for r in pub] == ["Red", "Blue"]
    assert set(pub[0]["breakdown"]) >= {
        "jeopardy",
        "attack_defense",
        "weighted",
        "weights",
    }


def test_weight_change_is_versioned_snapshotted_and_audited(app):
    eid, admin, clients = mixed_game(app)
    with app.app_context():
        assert (
            ArenaScoreSnapshot.query.filter_by(event_id=eid, kind="round").count() == 1
        )
    r = admin.post(
        f"{API}/admin/events/{eid}/scoring/configs",
        json={"jeopardy_weight": 0.1, "ad_weight": 5, "reason": "A&D focused finals"},
    )
    assert r.status_code == 201, r.get_json()
    data = r.get_json()["data"]
    assert data["config"]["version"] == 1
    assert {c["key"] for c in data["changes"]} == {
        r_["key"] for r_ in board(app, eid).values()
    }
    b = board(app, eid)
    assert b["Red"]["score"] == round(0.1 * 100 + 5 * (1 - 5)) == -10
    assert b["Blue"]["score"] == round(5 * 13) == 65
    assert [n for n, _ in sorted(b.items(), key=lambda kv: kv[1]["rank"])] == [
        "Blue",
        "Red",
    ]
    with app.app_context():
        kinds = [
            s.kind
            for s in ArenaScoreSnapshot.query.filter_by(event_id=eid).order_by(
                ArenaScoreSnapshot.id
            )
        ]
        assert kinds == ["round", "config_before", "config_after"]
        before = ArenaScoreSnapshot.query.filter_by(
            event_id=eid, kind="config_before"
        ).one()
        assert {p["name"]: p["score"] for p in before.payload} == {
            "Red": 96,
            "Blue": 13,
        }
    versions = admin.get(f"{API}/admin/events/{eid}/scoring/configs").get_json()["data"]
    assert (
        versions["active"]["version"] == 1
        and versions["versions"][0]["reason"] == "A&D focused finals"
    )
    # History is immutable: the round snapshot still shows the original scores.
    snaps = admin.get(
        f"{API}/admin/events/{eid}/scoring/snapshots?kind=round"
    ).get_json()["data"]
    full = admin.get(
        f"{API}/admin/events/{eid}/scoring/snapshots/{snaps[0]['id']}"
    ).get_json()["data"]
    assert {p["name"]: p["score"] for p in full["standings"]} == {"Red": 96, "Blue": 13}
    rules = clients["Red"].get(f"{API}/events/{eid}/scoring").get_json()["data"]
    assert (
        rules["weights"] == {"jeopardy": 0.1, "attack_defense": 5.0}
        and rules["version"] == 1
    )


def test_config_validation_and_permissions(app):
    eid, admin, clients = mixed_game(app)
    for bad in (
        {"jeopardy_weight": -1, "reason": "x y"},
        {"ad_weight": "lots", "reason": "abc"},
        {"tie_break": "coinflip", "reason": "abc"},
        {"bogus": 1, "reason": "abc"},
        {"jeopardy_weight": 2},
    ):
        assert (
            admin.post(
                f"{API}/admin/events/{eid}/scoring/configs", json=bad
            ).status_code
            == 400
        ), bad
    assert (
        clients["Red"]
        .post(
            f"{API}/admin/events/{eid}/scoring/configs",
            json={"ad_weight": 9, "reason": "cheat"},
        )
        .status_code
        == 403
    )
    with app.app_context():
        gen_role(make_user("an"), "analyst", eid)
        gen_role(make_user("so"), "scoreboard_operator", eid)
    assert (
        login_as_user(app, name="an")
        .post(
            f"{API}/admin/events/{eid}/scoring/configs",
            json={"ad_weight": 2, "reason": "nope"},
        )
        .status_code
        == 403
    )
    assert (
        login_as_user(app, name="so")
        .post(
            f"{API}/admin/events/{eid}/scoring/configs",
            json={"ad_weight": 2, "reason": "ok now"},
        )
        .status_code
        == 201
    )


def test_weights_rejected_for_non_mixed_events_but_tie_break_allowed(app):
    with app.app_context():
        eid = gen_event("jeop").id
    admin = login_as_user(app, name="admin")
    assert (
        admin.post(
            f"{API}/admin/events/{eid}/scoring/configs",
            json={"jeopardy_weight": 2, "reason": "nope"},
        ).status_code
        == 400
    )
    assert (
        admin.post(
            f"{API}/admin/events/{eid}/scoring/configs",
            json={"tie_break": "solves", "reason": "more solves wins"},
        ).status_code
        == 201
    )


def test_tie_break_rules(app):
    with app.app_context():
        eid = gen_event("tb", registration_open=True).id
    admin = login_as_user(app, name="admin")
    a = admin.post(
        f"{API}/admin/events/{eid}/challenges",
        json={
            "name": "a",
            "points": 100,
            "visible": True,
            "flags": [{"content": "fa"}],
        },
    ).get_json()["data"]
    b = admin.post(
        f"{API}/admin/events/{eid}/challenges",
        json={"name": "b", "points": 50, "visible": True, "flags": [{"content": "fb"}]},
    ).get_json()["data"]
    c = admin.post(
        f"{API}/admin/events/{eid}/challenges",
        json={"name": "c", "points": 50, "visible": True, "flags": [{"content": "fc"}]},
    ).get_json()["data"]
    p1 = login_as_user(app, name=_reg(app, eid, "one"))
    p1.post(
        f"{API}/events/{eid}/challenges/{a['id']}/attempt", json={"submission": "fa"}
    )  # 100, 1 solve, early
    p2 = login_as_user(app, name=_reg(app, eid, "two"))
    p2.post(
        f"{API}/events/{eid}/challenges/{b['id']}/attempt", json={"submission": "fb"}
    )
    p2.post(
        f"{API}/events/{eid}/challenges/{c['id']}/attempt", json={"submission": "fc"}
    )  # 100, 2 solves

    def order():
        return [
            r["name"] for r in sorted(board(app, eid).values(), key=lambda r: r["rank"])
        ]

    assert order() == ["one", "two"]  # default: earliest to reach the score
    admin.post(
        f"{API}/admin/events/{eid}/scoring/configs",
        json={"tie_break": "solves", "reason": "solves"},
    )
    assert order() == ["two", "one"]


def _reg(app, eid, name):
    with app.app_context():
        uid = make_user(name)
        gen_registration(eid, uid)
    return name


def test_history_snapshot_job(app):
    eid, _admin, _clients = mixed_game(app)
    with app.app_context():
        jobs.enqueue("scoring.history_snapshot")
    run_jobs(app)
    with app.app_context():
        assert (
            ArenaScoreSnapshot.query.filter_by(event_id=eid, kind="periodic").count()
            == 1
        )
        e = db.session.get(ArenaEvent, eid)
        e.ends_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        db.session.commit()
        for _ in range(2):  # final is taken once
            jobs.enqueue("scoring.history_snapshot")
            jobs.run_job(jobs.claim("t"), "t")
        assert (
            ArenaScoreSnapshot.query.filter_by(event_id=eid, kind="final").count() == 1
        )

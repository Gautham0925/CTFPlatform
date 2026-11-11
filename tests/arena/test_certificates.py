"""Phase 12: optional certificates."""

import datetime

import pytest

from CTFd.config import TestingConfig
from CTFd.models import db
from CTFd.plugins.arena.models import ArenaCertificate, ArenaEvent
from CTFd.plugins.arena.services import jobs
from CTFd.plugins.arena.worker import PERIODIC
from tests.arena.helpers import (
    create_arena_ctfd,
    gen_event,
    gen_registration,
    gen_role,
    make_user,
)
from tests.helpers import destroy_ctfd, login_as_user

API = "/api/v1/arena"


class EnabledConfig(TestingConfig):
    ARENA_CERTIFICATES_ENABLED = True


@pytest.fixture
def app():
    app = create_arena_ctfd(config=EnabledConfig)
    app.config["RATELIMIT_LOGIN"] = (1000, 5)
    yield app
    destroy_ctfd(app)


def run_jobs(app):
    with app.app_context():
        while (j := jobs.claim("t")) is not None:
            jobs.run_job(j, "t")


def finish(app, eid):
    with app.app_context():
        e = db.session.get(ArenaEvent, eid)
        e.status = "completed"
        db.session.commit()


def game(app, **event_kw):
    with app.app_context():
        eid = gen_event("cert", **event_kw).id
    admin = login_as_user(app, name="admin")
    ch = admin.post(
        f"{API}/admin/events/{eid}/challenges",
        json={"name": "c", "points": 100, "visible": True, "flags": [{"content": "f"}]},
    ).get_json()["data"]
    return eid, admin, ch


def player(app, eid, name, solve=None):
    with app.app_context():
        uid = make_user(name)
        gen_registration(eid, uid)
    c = login_as_user(app, name=name)
    if solve:
        eid_, ch = solve
        r = c.post(
            f"{API}/events/{eid_}/challenges/{ch['id']}/attempt",
            json={"submission": "f"},
        )
        assert r.get_json()["data"]["status"] == "correct"
    return uid, c


def test_disabled_by_default_is_fully_inert():
    app = create_arena_ctfd()  # default config
    try:
        assert app.extensions["arena"]["settings"].certificates_enabled is False
        admin = login_as_user(app, name="admin")
        with app.app_context():
            eid = gen_event("e").id
        assert (
            admin.get(f"{API}/admin/events/{eid}/certificates/templates").status_code
            == 404
        )
        assert (
            app.test_client().get(f"{API}/certificates/verify/x?code=y").status_code
            == 404
        )
        assert not any(
            "cert" in kind for _n, _i, kind, _p in PERIODIC
        )  # no background jobs
        # Competition completion is unaffected by the disabled certificate module.
        r = admin.post(f"{API}/admin/events/{eid}/status", json={"status": "completed"})
        assert r.status_code == 200
    finally:
        destroy_ctfd(app)


def test_issue_requires_completed_event_and_renders_asynchronously(app):
    eid, admin, ch = game(app)
    u1, c1 = player(app, eid, "solver", solve=(eid, ch))
    u2, c2 = player(app, eid, "idle")
    tpl = admin.post(
        f"{API}/admin/events/{eid}/certificates/templates",
        json={"name": "Participation", "kind": "participation"},
    ).get_json()["data"]
    r = admin.post(
        f"{API}/admin/events/{eid}/certificates/templates/{tpl['id']}/issue", json={}
    )
    assert r.status_code == 409 and r.get_json()["code"] == "event_not_completed"
    finish(app, eid)
    r = admin.post(
        f"{API}/admin/events/{eid}/certificates/templates/{tpl['id']}/issue", json={}
    )
    assert r.status_code == 202
    with app.app_context():
        assert ArenaCertificate.query.count() == 0  # nothing rendered in the request
    run_jobs(app)
    with app.app_context():
        certs = ArenaCertificate.query.all()
        assert [c.user_id for c in certs] == [u1]  # idle participant not eligible
        assert certs[0].status == "issued"
    mine = c1.get(f"{API}/events/{eid}/my-certificates").get_json()["data"]
    assert len(mine) == 1
    pdf = c1.get(mine[0]["download_url"])
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")
    assert c2.get(mine[0]["download_url"]).status_code == 404  # not the owner
    assert app.test_client().get(mine[0]["download_url"]).status_code == 401
    assert c2.get(f"{API}/events/{eid}/my-certificates").get_json()["data"] == []
    # Re-issuing is idempotent.
    admin.post(
        f"{API}/admin/events/{eid}/certificates/templates/{tpl['id']}/issue", json={}
    )
    run_jobs(app)
    with app.app_context():
        assert ArenaCertificate.query.count() == 1


def test_verify_and_revoke(app):
    eid, admin, ch = game(app)
    _u, c = player(app, eid, "solver", solve=(eid, ch))
    tpl = admin.post(
        f"{API}/admin/events/{eid}/certificates/templates",
        json={"name": "Winner", "kind": "winner"},
    ).get_json()["data"]
    finish(app, eid)
    admin.post(
        f"{API}/admin/events/{eid}/certificates/templates/{tpl['id']}/issue", json={}
    )
    run_jobs(app)
    with app.app_context():
        cert = ArenaCertificate.query.one()
        pid, code, db_id = cert.public_id, cert.verify_code, cert.id
    pub = app.test_client()
    good = pub.get(f"{API}/certificates/verify/{pid}?code={code}").get_json()["data"]
    assert good["valid"] is True and good["recipient"] == "solver" and good["rank"] == 1
    assert pub.get(f"{API}/certificates/verify/{pid}?code=wrong").get_json()[
        "data"
    ] == {"valid": False}
    r = admin.post(
        f"{API}/admin/events/{eid}/certificates/{db_id}/revoke",
        json={"reason": "cheating"},
    )
    assert r.status_code == 200
    after = pub.get(f"{API}/certificates/verify/{pid}?code={code}").get_json()["data"]
    assert after["valid"] is False and after["status"] == "revoked"
    assert c.get(f"{API}/certificates/{pid}/download").status_code == 404


def test_team_and_top_n_and_custom_eligibility(app):
    eid, admin, ch = game(app, team_mode="team", team_size_max=3)
    _a, ca = player(app, eid, "a")
    _b, cb = player(app, eid, "b")
    out_uid = None
    with app.app_context():
        out_uid = make_user("outsider")
    code = ca.post(f"{API}/events/{eid}/teams", json={"name": "Squad"}).get_json()[
        "data"
    ]["join_code"]
    cb.post(f"{API}/events/{eid}/teams/join", json={"join_code": code})
    ca.post(
        f"{API}/events/{eid}/challenges/{ch['id']}/attempt", json={"submission": "f"}
    )
    team_tpl = admin.post(
        f"{API}/admin/events/{eid}/certificates/templates",
        json={"name": "Team", "kind": "team"},
    ).get_json()["data"]
    top = admin.post(
        f"{API}/admin/events/{eid}/certificates/templates",
        json={"name": "Top", "kind": "top_n", "criteria": {"top_n": 1}},
    ).get_json()["data"]
    custom = admin.post(
        f"{API}/admin/events/{eid}/certificates/templates",
        json={
            "name": "Volunteer",
            "kind": "custom",
            "criteria": {"user_ids": [_b, out_uid]},
        },
    ).get_json()["data"]
    finish(app, eid)
    for t in (team_tpl, top, custom):
        admin.post(
            f"{API}/admin/events/{eid}/certificates/templates/{t['id']}/issue", json={}
        )
    run_jobs(app)
    with app.app_context():
        by_tpl = {}
        for c in ArenaCertificate.query.all():
            by_tpl.setdefault(c.template_id, []).append(c)
        assert [c.team_name for c in by_tpl[team_tpl["id"]]] == ["Squad"]
        assert sorted(c.user_id for c in by_tpl[top["id"]]) == sorted(
            [_a, _b]
        )  # both members
        assert [c.user_id for c in by_tpl[custom["id"]]] == [
            _b
        ]  # outsider not registered
    team_cert = [
        c
        for c in cb.get(f"{API}/events/{eid}/my-certificates").get_json()["data"]
        if c["team"]
    ]
    assert team_cert and cb.get(team_cert[0]["download_url"]).status_code == 200


def test_template_validation_and_rbac(app):
    eid, admin, _ch = game(app)
    for bad in (
        {"name": "x", "kind": "gold"},
        {"name": "x", "kind": "custom", "body": "{secret}"},
        {"name": "x", "kind": "top_n", "criteria": {"top_n": -1}},
        {"name": "x", "kind": "winner", "accent_color": "red"},
    ):
        assert (
            admin.post(
                f"{API}/admin/events/{eid}/certificates/templates", json=bad
            ).status_code
            == 400
        )
    with app.app_context():
        gen_role(make_user("cm"), "cert_manager", eid)
        gen_role(make_user("cmgr"), "challenge_manager", eid)
    assert (
        login_as_user(app, name="cm")
        .post(
            f"{API}/admin/events/{eid}/certificates/templates",
            json={"name": "ok", "kind": "winner"},
        )
        .status_code
        == 201
    )
    assert (
        login_as_user(app, name="cmgr")
        .post(
            f"{API}/admin/events/{eid}/certificates/templates",
            json={"name": "no", "kind": "winner"},
        )
        .status_code
        == 403
    )


def test_disabled_job_does_nothing():
    app = create_arena_ctfd()
    try:
        with app.app_context():
            jobs.enqueue("certificates.issue", {"template_id": 1})
            assert jobs.run_job(jobs.claim("t"), "t") == "done"
            assert ArenaCertificate.query.count() == 0
    finally:
        destroy_ctfd(app)


del datetime

"""Phase 11: external lab integration (MockProvider + GenericHTTPProvider)."""

import datetime
import threading
import time

import pytest
from flask import Flask, jsonify, request
from werkzeug.serving import make_server

from CTFd.models import db
from CTFd.plugins.arena.labs.base import ProviderError, ProviderTimeout
from CTFd.plugins.arena.labs.http import GenericHTTPProvider
from CTFd.plugins.arena.labs.mock import MockProvider
from CTFd.plugins.arena.models import ArenaLabProvider, ArenaLabSession, ArenaScoreEvent
from CTFd.plugins.arena.services import jobs
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
    app.config["ARENA_LAB_ALLOWED_PRIVATE_HOSTS"] = "127.0.0.1"
    yield app
    destroy_ctfd(app)


def run_jobs(app, later=False):
    def now():  # fresh each claim, like the real worker
        shift = datetime.timedelta(hours=1) if later else datetime.timedelta(0)
        return datetime.datetime.utcnow() + shift

    with app.app_context():
        while (j := jobs.claim("t", now=now())) is not None:
            jobs.run_job(j, "t")


def setup(app, provider_body=None, template="linux-101"):
    admin = login_as_user(app, name="admin")
    with app.app_context():
        eid = gen_event("labs").id
    body = provider_body or {"name": "mock", "adapter": "mock"}
    r = admin.post(f"{API}/admin/platform/lab-providers", json=body)
    assert r.status_code == 201, r.get_json()
    prov = r.get_json()["data"]
    lab = admin.post(
        f"{API}/admin/events/{eid}/labs",
        json={
            "provider_id": prov["id"],
            "template_id": template,
            "name": "Linux",
            "completion_points": 40,
        },
    ).get_json()["data"]
    return admin, eid, prov, lab


def player(app, name, eid):
    with app.app_context():
        uid = make_user(name)
        gen_registration(eid, uid)
    return uid, login_as_user(app, name=name)


def test_mock_provider_lifecycle_offline(app):
    admin, eid, prov, lab = setup(app)
    tested = admin.post(
        f"{API}/admin/platform/lab-providers/{prov['id']}/test", json={}
    ).get_json()["data"]
    assert tested["health"]["ok"] is True and {
        lab_["id"] for lab_ in tested["labs"]
    } >= {"linux-101"}
    _uid, c = player(app, "p", eid)
    r = c.post(f"{API}/events/{eid}/labs/{lab['id']}/session", json={})
    assert r.status_code == 202 and "connection" not in r.get_json()["data"]
    assert (
        c.post(f"{API}/events/{eid}/labs/{lab['id']}/session", json={}).status_code
        == 200
    )  # same session
    run_jobs(app)
    s = c.get(f"{API}/events/{eid}/labs/{lab['id']}/session").get_json()["data"]
    assert s["status"] == "ready" and s["connection"]["type"] == "ssh"
    assert "external_id" not in s and "uuid" not in s
    assert (
        c.post(
            f"{API}/events/{eid}/labs/{lab['id']}/session/reset", json={}
        ).status_code
        == 202
    )
    run_jobs(app)
    assert (
        c.delete(f"{API}/events/{eid}/labs/{lab['id']}/session", json={}).status_code
        == 200
    )
    run_jobs(app)
    with app.app_context():
        sess = ArenaLabSession.query.one()
        assert sess.status == "released" and sess.active_key is None
        assert sess.external_id not in MockProvider._sessions


def test_completion_is_server_verified_and_scored_once(app):
    admin, eid, prov, lab = setup(app)
    uid, c = player(app, "p", eid)
    c.post(f"{API}/events/{eid}/labs/{lab['id']}/session", json={})
    run_jobs(app)
    # No client-side way to claim completion.
    assert c.post(
        f"{API}/events/{eid}/labs/{lab['id']}/complete", json={}
    ).status_code in (404, 405)
    with app.app_context():
        MockProvider.mark_completed(ArenaLabSession.query.one().external_id)
        for _ in range(2):
            jobs.enqueue("labs.sync")
            jobs.run_job(jobs.claim("t"), "t")
        rows = ArenaScoreEvent.query.filter_by(
            event_id=eid, source="lab_completion"
        ).all()
        assert (
            len(rows) == 1 and rows[0].points == 40 and rows[0].entity_key == f"u:{uid}"
        )
    assert (
        c.get(f"{API}/events/{eid}/labs").get_json()["data"][0]["session"]["completed"]
        is True
    )


def test_expiry_cleans_up(app):
    admin, eid, prov, lab = setup(app)
    _uid, c = player(app, "p", eid)
    c.post(f"{API}/events/{eid}/labs/{lab['id']}/session", json={})
    run_jobs(app)
    with app.app_context():
        sess = ArenaLabSession.query.one()
        ext = sess.external_id
        sess.expires_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        db.session.commit()
        jobs.enqueue("labs.expire")
    run_jobs(app)
    with app.app_context():
        assert ArenaLabSession.query.one().status == "expired"
    assert ext not in MockProvider._sessions


def test_provider_failure_never_breaks_requests(app):
    admin, eid, prov, lab = setup(
        app, {"name": "broken", "adapter": "mock", "config": {"simulate_failure": True}}
    )
    _uid, c = player(app, "p", eid)
    assert (
        c.post(f"{API}/events/{eid}/labs/{lab['id']}/session", json={}).status_code
        == 202
    )
    for _ in range(5):
        run_jobs(app, later=True)
    with app.app_context():
        s = ArenaLabSession.query.one()
        assert s.status == "failed" and s.active_key is None
    got = c.get(f"{API}/events/{eid}/labs").get_json()["data"][0]["session"]
    assert got is None  # slot freed; participant may retry
    r = admin.post(f"{API}/admin/platform/lab-providers/{prov['id']}/test", json={})
    assert r.status_code == 200 and r.get_json()["data"]["health"]["ok"] is False


def test_credentials_are_write_only_and_encrypted(app):
    admin = login_as_user(app, name="admin")
    r = admin.post(
        f"{API}/admin/platform/lab-providers",
        json={
            "name": "remote",
            "adapter": "generic_http",
            "base_url": "http://127.0.0.1:1",
            "token": "TOP-SECRET-TOKEN",
        },
    )
    data = r.get_json()["data"]
    assert data["has_credentials"] is True and "TOP-SECRET" not in r.get_data(
        as_text=True
    )
    listing = admin.get(f"{API}/admin/platform/lab-providers").get_data(as_text=True)
    assert "TOP-SECRET" not in listing
    with app.app_context():
        row = db.session.get(ArenaLabProvider, data["id"])
        assert "TOP-SECRET" not in row.credentials_enc


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest",  # metadata (and plain http)
        "https://localhost/api",  # loopback
        "https://10.0.0.5/api",  # private
        "http://example.com/api",  # not https
        "https://user:pw@example.com/",  # embedded credentials
    ],
)
def test_ssrf_protection_on_configuration(app, url):
    app.config["ARENA_LAB_ALLOWED_PRIVATE_HOSTS"] = ""
    admin = login_as_user(app, name="admin")
    r = admin.post(
        f"{API}/admin/platform/lab-providers",
        json={"name": "xx", "adapter": "generic_http", "base_url": url},
    )
    assert r.status_code == 400 and r.get_json()["code"] == "invalid:base_url"


def test_access_control(app):
    admin, eid, prov, lab = setup(app)
    _u1, c1 = player(app, "p1", eid)
    _u2, c2 = player(app, "p2", eid)
    c1.post(f"{API}/events/{eid}/labs/{lab['id']}/session", json={})
    run_jobs(app)
    assert (
        c2.get(f"{API}/events/{eid}/labs/{lab['id']}/session").get_json()["data"]
        is None
    )  # not p1's
    with app.app_context():
        other = gen_event("other").id
        gen_role(make_user("org"), "organizer", other)
    assert c1.get(f"{API}/events/{other}/labs").status_code == 403
    org = login_as_user(app, name="org")
    assert org.get(f"{API}/admin/events/{eid}/labs/sessions").status_code == 403
    assert (
        org.post(
            f"{API}/admin/platform/lab-providers", json={"name": "z", "adapter": "mock"}
        ).status_code
        == 403
    )
    assert admin.get(f"{API}/admin/events/{eid}/labs/sessions").get_json()["data"][0][
        "external_id"
    ]


# --------------------------------------------------------- generic HTTP


@pytest.fixture
def fake_provider():
    state = {"sessions": {}, "auth": [], "slow": False}
    papp = Flask("fake-provider")

    @papp.before_request
    def auth():
        state["auth"].append(request.headers.get("Authorization"))
        if request.headers.get("Authorization") != "Bearer good-token":
            return jsonify({"error": "unauthorized"}), 401
        if state["slow"]:
            time.sleep(2)
        return None

    @papp.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @papp.get("/labs")
    def labs():
        return jsonify({"labs": [{"id": "web-1", "name": "Web 1"}, {"bad": True}]})

    @papp.post("/sessions")
    def create():
        body = request.get_json()
        for sid, s in state["sessions"].items():
            if s["session_id"] == body["session_id"]:
                return jsonify({"id": sid, "status": s["status"]})
        sid = f"ext-{len(state['sessions']) + 1}"
        state["sessions"][sid] = {
            "session_id": body["session_id"],
            "status": "ready",
            "completed": False,
        }
        return jsonify({"id": sid, "status": "ready"}), 201

    @papp.get("/sessions/<sid>")
    def status(sid):
        s = state["sessions"].get(sid)
        if s is None:
            return jsonify({}), 404
        return jsonify({"id": sid, "status": s["status"], "completed": s["completed"]})

    @papp.get("/sessions/<sid>/connection")
    def connection(sid):
        return jsonify(
            {
                "type": "url",
                "url": "javascript:alert(1)",
                "host": "lab.example",
                "port": 443,
                "password": "p" * 1000,
                "evil_field": "x",
            }
        )

    @papp.delete("/sessions/<sid>")
    def delete(sid):
        state["sessions"].pop(sid, None)
        return "", 204

    @papp.get("/redirect/health")
    def redirect_health():
        return "", 302, {"Location": "http://169.254.169.254/"}

    server = make_server("127.0.0.1", 0, papp, threaded=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()


def test_generic_http_provider_contract(app, fake_provider):
    url, state = fake_provider
    admin, eid, prov, lab = setup(
        app,
        {
            "name": "http",
            "adapter": "generic_http",
            "base_url": url,
            "token": "good-token",
        },
        template="web-1",
    )
    tested = admin.post(
        f"{API}/admin/platform/lab-providers/{prov['id']}/test", json={}
    ).get_json()["data"]
    assert tested["health"]["ok"] and tested["labs"] == [
        {"id": "web-1", "name": "Web 1", "description": ""}
    ]
    _uid, c = player(app, "p", eid)
    c.post(f"{API}/events/{eid}/labs/{lab['id']}/session", json={})
    run_jobs(app)
    s = c.get(f"{API}/events/{eid}/labs/{lab['id']}/session").get_json()["data"]
    assert s["status"] == "ready"
    conn = s["connection"]
    assert "url" not in conn  # javascript: URL dropped
    assert (
        len(conn["password"]) == 256 and "evil_field" not in conn
    )  # untrusted data cleaned
    assert set(state["auth"]) == {"Bearer good-token"}
    with app.app_context():
        ext = ArenaLabSession.query.one().external_id
    state["sessions"][ext]["completed"] = True
    with app.app_context():
        jobs.enqueue("labs.sync")
        jobs.run_job(jobs.claim("t"), "t")
        assert ArenaScoreEvent.query.filter_by(source="lab_completion").count() == 1
    c.delete(f"{API}/events/{eid}/labs/{lab['id']}/session", json={})
    run_jobs(app)
    assert state["sessions"] == {}


def test_generic_http_timeouts_auth_and_redirects(app, fake_provider):
    url, state = fake_provider
    with app.app_context():
        p = GenericHTTPProvider(
            url, {"token": "good-token"}, {"read_timeout": 0.5, "retries": 1}
        )
        state["slow"] = True
        start = time.monotonic()
        with pytest.raises(ProviderTimeout):
            p.health()
        assert time.monotonic() - start < 3  # 2 attempts x 0.5 s + backoff
        state["slow"] = False
        with pytest.raises(ProviderError, match="rejected"):
            GenericHTTPProvider(url, {"token": "wrong"}, {}).health()
        with pytest.raises(ProviderError, match="redirect"):
            GenericHTTPProvider(url + "/redirect", {"token": "good-token"}, {}).health()
        assert p.status("missing") == {"status": "expired", "completed": False}


def test_generic_http_blocks_rebinding_at_request_time(app, fake_provider):
    url, _state = fake_provider
    with app.app_context():
        p = GenericHTTPProvider(url, {"token": "good-token"}, {})
        assert p.health()["status"] == "ok"
        app.config["ARENA_LAB_ALLOWED_PRIVATE_HOSTS"] = ""  # host no longer allowed
        with pytest.raises(ProviderError, match="blocked"):
            p.health()

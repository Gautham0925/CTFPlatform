"""Phase 8: challenge execution (platform side + runner interop)."""

import datetime
import pathlib
import sys
import threading

import pytest
from werkzeug.serving import make_server

from CTFd.models import Users, db
from CTFd.plugins.arena.execution import client as runner_client
from CTFd.plugins.arena.execution.client import HTTPRunnerClient, MockRunnerClient
from CTFd.plugins.arena.models import ArenaEvent, ArenaEventChallenge, ArenaInstance
from CTFd.plugins.arena.services import entity as entity_service
from CTFd.plugins.arena.services import instances as inst_service
from CTFd.plugins.arena.services import jobs
from tests.arena.helpers import (
    create_arena_ctfd,
    gen_event,
    gen_registration,
    gen_role,
    make_user,
)
from tests.helpers import destroy_ctfd, login_as_user

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runner"))
from arena_runner.app import create_app as create_runner_app  # noqa: E402
from arena_runner.backends.mock import MockBackend  # noqa: E402
from arena_runner.config import RunnerConfig  # noqa: E402
from arena_runner.store import Store  # noqa: E402

API = "/api/v1/arena"
SECRET = "r" * 48
IMG = "registry.local/web@sha256:" + "c" * 64


@pytest.fixture
def app():
    runner_client.set_client(None)
    app = create_arena_ctfd()
    app.config["RATELIMIT_LOGIN"] = (1000, 5)
    yield app
    runner_client.set_client(None)
    destroy_ctfd(app)


@pytest.fixture
def live_runner():
    cfg = RunnerConfig(
        keys={"platform": SECRET},
        backend="mock",
        allowed_images=frozenset({IMG}),
        public_host="203.0.113.10",
        port_min=32000,
        port_max=32050,
        max_instances=10,
        db_path=":memory:",
    )
    backend = MockBackend(cfg)
    rapp = create_runner_app(cfg, backend=backend, store=Store(":memory:"))
    server = make_server("127.0.0.1", 0, rapp, threaded=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{server.server_port}"
    client = HTTPRunnerClient(url, "platform", SECRET, timeout=(2, 5))
    runner_client.set_client(client)
    yield client, backend
    server.shutdown()
    runner_client.set_client(None)


def run_jobs(app, kinds=None):
    with app.app_context():
        n = 0
        while (j := jobs.claim("test", kinds=kinds)) is not None:
            jobs.run_job(j, "test")
            n += 1
        return n


def setup(app, **event_kw):
    admin = login_as_user(app, name="admin")
    with app.app_context():
        eid = gen_event("e", **event_kw).id
    img = admin.post(
        f"{API}/admin/platform/images",
        json={
            "name": "web",
            "version": "1",
            "reference": IMG,
            "ports": [80],
            "memory_mb": 128,
            "ttl_seconds": 1800,
        },
    ).get_json()["data"]
    ch = admin.post(
        f"{API}/admin/events/{eid}/challenges",
        json={
            "name": "inst",
            "points": 100,
            "visible": True,
            "image_id": img["id"],
            "per_entity_flag": True,
        },
    ).get_json()["data"]
    return admin, eid, img, ch


def player(app, name, eid):
    with app.app_context():
        uid = make_user(name)
        gen_registration(eid, uid)
    return uid, login_as_user(app, name=name)


def test_signing_copies_are_identical():
    a = (ROOT / "runner/arena_runner/signing.py").read_text()
    b = (ROOT / "CTFd/plugins/arena/execution/signing.py").read_text()
    assert b.split("\n", 2)[2] == a  # identical except the 2-line provenance header


def test_platform_never_imports_docker():
    for path in (ROOT / "CTFd/plugins/arena").rglob("*.py"):
        text = path.read_text()
        assert "import docker" not in text and "docker.sock" not in text, path


def test_end_to_end_with_live_runner(app, live_runner):
    client, backend = live_runner
    admin, eid, img, ch = setup(app)
    assert (
        admin.post(
            f"{API}/admin/platform/images/{img['id']}/validate", json={}
        ).status_code
        == 200
    )
    uid, c = player(app, "p", eid)
    r = c.post(f"{API}/events/{eid}/challenges/{ch['id']}/instance", json={})
    assert r.status_code == 202 and r.get_json()["data"]["status"] == "requested"
    again = c.post(f"{API}/events/{eid}/challenges/{ch['id']}/instance", json={})
    assert (
        again.status_code == 200
        and again.get_json()["data"]["id"] == r.get_json()["data"]["id"]
    )
    run_jobs(app, kinds=["instances.provision"])
    d = c.get(f"{API}/events/{eid}/challenges/{ch['id']}/instance").get_json()["data"]
    assert d["status"] == "running" and d["host"] == "203.0.113.10"
    assert 32000 <= d["ports"]["80"] <= 32050
    assert (
        "uuid" not in d and "entity_key" not in d
    )  # no internal details to participants
    (container,) = backend.containers.values()
    with app.app_context():
        e = db.session.get(ArenaEvent, eid)
        ec = db.session.get(ArenaEventChallenge, ch["id"])
        from CTFd.plugins.arena.services.submissions import per_entity_flag

        expected_flag = per_entity_flag(e, ec, entity_service.user_key(uid))
    assert container["spec"].env == {"ARENA_FLAG": expected_flag}
    assert container["spec"].memory_mb == 128
    # The per-entity flag delivered to the instance solves the challenge.
    r = c.post(
        f"{API}/events/{eid}/challenges/{ch['id']}/attempt",
        json={"submission": expected_flag},
    )
    assert r.get_json()["data"]["status"] == "correct"
    assert (
        c.delete(
            f"{API}/events/{eid}/challenges/{ch['id']}/instance", json={}
        ).status_code
        == 200
    )
    run_jobs(app, kinds=["instances.destroy"])
    assert backend.containers == {}
    assert (
        c.get(f"{API}/events/{eid}/challenges/{ch['id']}/instance").get_json()["data"]
        is None
    )


def test_expiry_destroys_instances(app, live_runner):
    _client, backend = live_runner
    admin, eid, img, ch = setup(app)
    _uid, c = player(app, "p", eid)
    c.post(f"{API}/events/{eid}/challenges/{ch['id']}/instance", json={})
    run_jobs(app)
    assert len(backend.containers) == 1
    with app.app_context():
        inst = ArenaInstance.query.one()
        inst.expires_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        db.session.commit()
        jobs.enqueue("instances.expire")
    run_jobs(app)
    assert backend.containers == {}
    with app.app_context():
        inst = ArenaInstance.query.one()
        assert inst.status == "expired" and inst.active_key is None


def test_runner_down_retries_then_frees_slot(app):
    runner_client.set_client(
        HTTPRunnerClient(
            "http://127.0.0.1:9", "k", SECRET, timeout=(0.2, 0.2), retries=0
        )
    )
    admin, eid, img, ch = setup(app)
    _uid, c = player(app, "p", eid)
    assert (
        c.post(
            f"{API}/events/{eid}/challenges/{ch['id']}/instance", json={}
        ).status_code
        == 202
    )
    with app.app_context():
        later = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
        for _ in range(10):
            j = jobs.claim("t", now=later)
            if j is None:
                break
            jobs.run_job(j, "t")
        inst = ArenaInstance.query.one()
        assert inst.status == "failed" and inst.active_key is None
    # The participant can try again once the runner is back.
    runner_client.set_client(MockRunnerClient())
    assert (
        c.post(
            f"{API}/events/{eid}/challenges/{ch['id']}/instance", json={}
        ).status_code
        == 202
    )


def test_quotas_and_access_control(app):
    runner_client.set_client(MockRunnerClient())
    admin, eid, img, ch = setup(app, settings={"max_instances_per_entity": 1})
    ch2 = admin.post(
        f"{API}/admin/events/{eid}/challenges",
        json={"name": "inst2", "points": 10, "visible": True, "image_id": img["id"]},
    ).get_json()["data"]
    plain = admin.post(
        f"{API}/admin/events/{eid}/challenges",
        json={"name": "plain", "points": 10, "visible": True},
    ).get_json()["data"]
    hidden = admin.post(
        f"{API}/admin/events/{eid}/challenges",
        json={"name": "hid", "points": 10, "visible": False, "image_id": img["id"]},
    ).get_json()["data"]
    _uid, c = player(app, "p", eid)
    assert (
        c.post(
            f"{API}/events/{eid}/challenges/{ch['id']}/instance", json={}
        ).status_code
        == 202
    )
    r = c.post(f"{API}/events/{eid}/challenges/{ch2['id']}/instance", json={})
    assert r.status_code == 429 and r.get_json()["code"] == "quota"
    assert (
        c.post(
            f"{API}/events/{eid}/challenges/{plain['id']}/instance", json={}
        ).get_json()["code"]
        == "no_instance"
    )
    assert (
        c.post(
            f"{API}/events/{eid}/challenges/{hidden['id']}/instance", json={}
        ).status_code
        == 404
    )
    with app.app_context():
        other = gen_event("other").id
        outsider = make_user("out")
        gen_registration(other, outsider)
    oc = login_as_user(app, name="out")
    assert (
        oc.post(
            f"{API}/events/{eid}/challenges/{ch['id']}/instance", json={}
        ).status_code
        == 403
    )
    assert (
        oc.post(
            f"{API}/events/{other}/challenges/{ch['id']}/instance", json={}
        ).status_code
        == 404
    )
    with app.app_context():
        gen_role(make_user("org"), "organizer", eid)
    org = login_as_user(app, name="org")
    assert (
        org.post(f"{API}/admin/platform/images", json={"name": "x"}).status_code == 403
    )
    assert org.get(f"{API}/admin/events/{eid}/instances").status_code == 200


def test_image_validation_rules(app):
    admin = login_as_user(app, name="admin")
    bad = admin.post(
        f"{API}/admin/platform/images",
        json={
            "name": "web",
            "version": "1",
            "reference": "nginx:latest",
            "ports": [80],
        },
    )
    assert bad.status_code == 400 and bad.get_json()["code"] == "invalid:reference"
    assert (
        admin.post(
            f"{API}/admin/platform/images",
            json={"name": "web", "version": "1", "reference": IMG, "ports": [70000]},
        ).status_code
        == 400
    )


def test_runner_rejects_non_allowlisted_image(app, live_runner):
    admin = login_as_user(app, name="admin")
    other = "registry.local/evil@sha256:" + "d" * 64
    img = admin.post(
        f"{API}/admin/platform/images",
        json={"name": "evil", "version": "1", "reference": other, "ports": [80]},
    ).get_json()["data"]
    r = admin.post(f"{API}/admin/platform/images/{img['id']}/validate", json={})
    assert r.status_code == 409


def test_remote_mode_requires_https(app):
    app.config.update(
        ARENA_RUNNER_URL="http://runner.internal",
        ARENA_RUNNER_KEY_ID="k",
        ARENA_RUNNER_SECRET=SECRET,
    )
    with app.app_context():
        from dataclasses import replace

        settings = app.extensions["arena"]["settings"]
        app.extensions["arena"]["settings"] = replace(settings, runner_mode="remote")
        try:
            with pytest.raises(runner_client.RunnerError, match="https"):
                runner_client.get_client()
            app.config["ARENA_RUNNER_URL"] = "https://runner.internal"
            assert isinstance(runner_client.get_client(), HTTPRunnerClient)
        finally:
            app.extensions["arena"]["settings"] = settings


def test_concurrent_instance_requests_one_active(app):
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        pytest.skip("needs MariaDB/MySQL")
    runner_client.set_client(MockRunnerClient())
    admin, eid, img, ch = setup(app, settings={"max_instances_per_entity": 5})
    with app.app_context():
        uid = make_user("p")
        gen_registration(eid, uid)
    barrier = threading.Barrier(15, timeout=30)
    ids, lock = [], threading.Lock()

    def go():
        barrier.wait()
        with app.app_context():
            try:
                e = db.session.get(ArenaEvent, eid)
                u = db.session.get(Users, uid)
                inst, _created = inst_service.request_instance(
                    e,
                    db.session.get(ArenaEventChallenge, ch["id"]),
                    entity_service.resolve(e, u),
                    u,
                )
                with lock:
                    ids.append(inst.id)
            finally:
                db.session.remove()

    ts = [threading.Thread(target=go) for _ in range(15)]
    [t.start() for t in ts]
    [t.join(timeout=60) for t in ts]
    assert len(ids) == 15 and len(set(ids)) == 1
    with app.app_context():
        assert (
            ArenaInstance.query.filter(ArenaInstance.active_key.isnot(None)).count()
            == 1
        )


# Real-container isolation tests live in runner/tests/test_docker_integration.py
# and run in the runner's own environment: the platform must never use Docker.


def test_released_instances_do_not_hold_quota(app):
    """Regression (Phase 16 instance burst): a released instance ends in
    status "stopped" with active_key cleared; it was still counted as active,
    so after max_instances_per_entity start/stop cycles the participant was
    locked out until the TTL expired."""
    runner_client.set_client(MockRunnerClient())
    _admin, eid, _img, ch = setup(app, settings={"max_instances_per_entity": 1})
    _uid, c = player(app, "p", eid)
    url = f"{API}/events/{eid}/challenges/{ch['id']}/instance"
    for cycle in range(4):
        r = c.post(url, json={})
        assert r.status_code == 202, (cycle, r.get_json())
        run_jobs(app)  # provision
        assert c.get(url).get_json()["data"]["status"] == "running"
        assert c.delete(url, json={}).status_code == 200
        run_jobs(app)  # destroy
    with app.app_context():
        rows = ArenaInstance.query.filter_by(event_id=eid).all()
        assert len(rows) == 4
        assert all(r.active_key is None and r.status == "stopped" for r in rows)
        # expiry must not resurrect released rows
        for r in rows:
            r.expires_at = datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
        db.session.commit()
        assert inst_service.expire_due() == 0
    # A paused-but-restartable instance (active_key kept) still holds the slot.
    assert c.post(url, json={}).status_code == 202
    run_jobs(app)
    with app.app_context():
        inst = ArenaInstance.query.filter(ArenaInstance.active_key.isnot(None)).one()
        inst.status = "stopped"
        db.session.commit()
    ch2 = _admin.post(
        f"{API}/admin/events/{eid}/challenges",
        json={"name": "inst2", "points": 10, "visible": True, "image_id": _img["id"]},
    ).get_json()["data"]
    r = c.post(f"{API}/events/{eid}/challenges/{ch2['id']}/instance", json={})
    assert r.status_code == 429 and r.get_json()["code"] == "quota"

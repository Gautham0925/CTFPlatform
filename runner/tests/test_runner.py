"""Runner service tests (no Docker daemon required)."""

import json
import threading
import time
import uuid

import pytest
from arena_runner import signing
from arena_runner.app import create_app, reap
from arena_runner.backends.base import BackendError, InstanceSpec
from arena_runner.backends.docker import DockerBackend, container_kwargs, network_kwargs
from arena_runner.backends.mock import MockBackend
from arena_runner.config import RunnerConfig
from arena_runner.store import Store

SECRET = "s" * 40
IMG = "registry.local/web-chal@sha256:" + "a" * 64
IMG2 = "registry.local/other@sha256:" + "b" * 64


def make_config(**kw):
    defaults = {
        "keys": {"k1": SECRET},
        "backend": "mock",
        "allowed_images": frozenset({IMG}),
        "public_host": "198.51.100.7",
        "port_min": 31000,
        "port_max": 31009,
        "max_instances": 8,
        "db_path": ":memory:",
        "bind_ip": "198.51.100.7",
    }
    defaults.update(kw)
    return RunnerConfig(**defaults)


@pytest.fixture
def runner():
    cfg = make_config()
    store = Store(":memory:")
    backend = MockBackend(cfg)
    app = create_app(cfg, backend=backend, store=store)
    return app, store, backend


def call(app, method, path, body=None, secret=SECRET, key_id="k1", **sign_kw):
    raw = json.dumps(body).encode() if body is not None else b""
    headers = signing.sign(secret, method, path, raw, key_id=key_id, **sign_kw)
    headers["Content-Type"] = "application/json"
    return app.test_client().open(path, method=method, data=raw, headers=headers)


def new_spec(**kw):
    data = {
        "instance_id": str(uuid.uuid4()),
        "image": IMG,
        "ports": [80],
        "ttl_seconds": 600,
        "cpu_millis": 500,
        "memory_mb": 256,
        "pids": 64,
        "env": {"ARENA_FLAG": "flag{x}"},
        "labels": {"event": "1", "entity": "t_3"},
    }
    data.update(kw)
    return data


# --------------------------------------------------------------- signing


def test_signature_roundtrip_and_rejections():
    store = Store(":memory:")
    keys = {"k1": SECRET}
    h = signing.sign(SECRET, "POST", "/v1/x", b"{}", key_id="k1")
    assert signing.verify(keys, "POST", "/v1/x", b"{}", h, store) == "k1"
    with pytest.raises(signing.SignatureError, match="replayed"):
        signing.verify(keys, "POST", "/v1/x", b"{}", h, store)
    for mutate in (
        lambda h: ("POST", "/v1/x", b'{"a":1}', h),  # body tampered
        lambda h: ("POST", "/v1/y", b"{}", h),  # path tampered
        lambda h: ("GET", "/v1/x", b"{}", h),  # method tampered
        lambda h: ("POST", "/v1/x", b"{}", {**h, signing.HEADER_KEY: "nope"}),
        lambda h: (
            "POST",
            "/v1/x",
            b"{}",
            {k: v for k, v in h.items() if k != signing.HEADER_SIG},
        ),
    ):
        fresh = signing.sign(SECRET, "POST", "/v1/x", b"{}", key_id="k1")
        with pytest.raises(signing.SignatureError):
            signing.verify(keys, *mutate(fresh), store)
    wrong = signing.sign("x" * 40, "POST", "/v1/x", b"{}", key_id="k1")
    with pytest.raises(signing.SignatureError, match="bad signature"):
        signing.verify(keys, "POST", "/v1/x", b"{}", wrong, store)
    old = signing.sign(
        SECRET, "POST", "/v1/x", b"{}", key_id="k1", timestamp=time.time() - 120
    )
    with pytest.raises(signing.SignatureError, match="skew"):
        signing.verify(keys, "POST", "/v1/x", b"{}", old, store)


def test_unsigned_and_badly_signed_requests_are_rejected(runner):
    app, _s, _b = runner
    c = app.test_client()
    assert c.get("/v1/health").status_code == 401
    assert call(app, "GET", "/v1/health", secret="z" * 40).status_code == 401
    assert call(app, "GET", "/v1/health", key_id="other").status_code == 401
    assert call(app, "GET", "/v1/health").status_code == 200


def test_replayed_request_rejected(runner):
    app, _s, _b = runner
    body = new_spec()
    raw = json.dumps(body).encode()
    headers = signing.sign(SECRET, "POST", "/v1/instances", raw, key_id="k1")
    headers["Content-Type"] = "application/json"
    c = app.test_client()
    assert c.post("/v1/instances", data=raw, headers=headers).status_code == 201
    assert c.post("/v1/instances", data=raw, headers=headers).status_code == 401


def test_query_string_is_signed(runner):
    app, _s, _b = runner
    assert call(app, "GET", "/v1/instances?label_event=1").status_code == 200
    raw_headers = signing.sign(SECRET, "GET", "/v1/instances", b"", key_id="k1")
    r = app.test_client().get("/v1/instances?label_event=2", headers=raw_headers)
    assert r.status_code == 401  # query not covered by signature -> rejected


# -------------------------------------------------------------- lifecycle


def test_instance_lifecycle_and_idempotency(runner):
    app, store, backend = runner
    spec = new_spec()
    r = call(app, "POST", "/v1/instances", spec)
    assert r.status_code == 201, r.get_json()
    d = r.get_json()
    assert d["status"] == "running" and d["host"] == "198.51.100.7"
    assert 31000 <= d["ports"]["80"] <= 31009
    again = call(app, "POST", "/v1/instances", spec)  # retry with same id
    assert again.status_code == 200 and again.get_json()["ports"] == d["ports"]
    assert len(backend.containers) == 1
    iid = spec["instance_id"]
    assert (
        call(app, "POST", f"/v1/instances/{iid}/stop").get_json()["status"] == "stopped"
    )
    assert (
        call(app, "POST", f"/v1/instances/{iid}/start").get_json()["status"]
        == "running"
    )
    assert call(app, "DELETE", f"/v1/instances/{iid}").get_json()["status"] == "removed"
    assert backend.containers == {}
    assert call(app, "DELETE", f"/v1/instances/{iid}").status_code == 200  # idempotent
    assert call(app, "GET", f"/v1/instances/{uuid.uuid4()}").status_code == 404


@pytest.mark.parametrize(
    "bad,status",
    [
        ({"image": "registry.local/web-chal:latest"}, 400),  # not digest pinned
        ({"image": IMG2}, 403),  # not allowlisted
        ({"instance_id": "not-a-uuid"}, 400),
        ({"memory_mb": 99999}, 400),
        ({"cpu_millis": 10}, 400),
        ({"ports": [0]}, 400),
        ({"env": {"LD_PRELOAD": "/evil.so"}}, 400),
        ({"env": {"lower": "x"}}, 400),
    ],
)
def test_create_validation(runner, bad, status):
    app, _s, backend = runner
    assert call(app, "POST", "/v1/instances", new_spec(**bad)).status_code == status
    assert backend.containers == {}


def test_ttl_is_capped(runner):
    app, store, _b = runner
    spec = new_spec(ttl_seconds=10**9)
    call(app, "POST", "/v1/instances", spec)
    row = store.get(spec["instance_id"])
    assert row["expires_at"] - row["created_at"] <= 4 * 3600 + 1


def test_backend_failure_is_reported_and_ports_released(runner):
    app, store, backend = runner
    backend.fail_next_create = True
    spec = new_spec()
    r = call(app, "POST", "/v1/instances", spec)
    assert r.status_code == 502 and r.get_json()["status"] == "failed"
    assert store.used_ports() == set()


def test_capacity_and_unique_ports_under_concurrency(runner):
    app, store, backend = runner
    results, lock = [], threading.Lock()
    barrier = threading.Barrier(20)

    def go():
        barrier.wait()
        r = call(app, "POST", "/v1/instances", new_spec())
        with lock:
            results.append((r.status_code, r.get_json()))

    ts = [threading.Thread(target=go) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ok = [d for s, d in results if s == 201]
    assert len(ok) == 8  # max_instances
    assert sum(1 for s, _ in results if s == 429) == 12
    ports = [d["ports"]["80"] for d in ok]
    assert len(set(ports)) == 8  # never the same host port twice


def test_reaper_expires_and_removes_orphans_after_restart(runner):
    app, store, backend = runner
    spec = new_spec(ttl_seconds=60)
    call(app, "POST", "/v1/instances", spec)
    keep = new_spec(ttl_seconds=3600)
    call(app, "POST", "/v1/instances", keep)
    backend.containers["arena-orphan"] = {
        "spec": None,
        "status": "running",
        "ports": {},
    }
    result = reap(store, backend, now=time.time() + 120)
    assert result == {"expired": 1, "orphans_removed": 1}
    assert store.get(spec["instance_id"])["status"] == "expired"
    assert set(backend.containers) == {f"mock-{keep['instance_id']}"}
    # "Restart": a new app over the same persistent store finds and keeps state.
    app2 = create_app(make_config(), backend=backend, store=store)
    assert (
        call(app2, "GET", f"/v1/instances/{keep['instance_id']}").get_json()["status"]
        == "running"
    )


def test_disappeared_container_is_marked_failed(runner):
    app, _store, backend = runner
    spec = new_spec()
    call(app, "POST", "/v1/instances", spec)
    backend.containers.clear()
    assert (
        call(app, "GET", f"/v1/instances/{spec['instance_id']}").get_json()["status"]
        == "failed"
    )


def test_image_check_endpoint(runner):
    app, _s, _b = runner
    assert (
        call(app, "POST", "/v1/images/check", {"image": IMG}).get_json()["allowed"]
        is True
    )
    assert (
        call(app, "POST", "/v1/images/check", {"image": IMG2}).get_json()["allowed"]
        is False
    )


def test_config_requires_keys_and_digest_pins():
    with pytest.raises(ValueError):
        RunnerConfig.from_env({})
    with pytest.raises(ValueError):
        RunnerConfig.from_env({"ARENA_RUNNER_KEYS": "k:short"})
    with pytest.raises(ValueError):
        RunnerConfig.from_env(
            {
                "ARENA_RUNNER_KEYS": f"k:{SECRET}",
                "ARENA_RUNNER_ALLOWED_IMAGES": "nginx:latest",
            }
        )
    cfg = RunnerConfig.from_env(
        {"ARENA_RUNNER_KEYS": f"k:{SECRET}", "ARENA_RUNNER_ALLOWED_IMAGES": IMG}
    )
    assert cfg.allowed_images == {IMG}


# ------------------------------------------------------- docker hardening


def spec_obj(**kw):
    d = {
        "instance_id": str(uuid.uuid4()),
        "image": IMG,
        "ports": [80],
        "cpu_millis": 500,
        "memory_mb": 256,
        "pids": 64,
        "ttl_seconds": 600,
        "env": {"ARENA_FLAG": "x"},
        "labels": {"event": "1"},
    }
    d.update(kw)
    return InstanceSpec(**d)


def test_container_kwargs_hardening():
    cfg = make_config(docker_runtime="runsc")
    k = container_kwargs(spec_obj(), cfg, {80: 31001})
    assert k["privileged"] is False
    assert k["cap_drop"] == ["ALL"] and k["cap_add"] == []
    assert "no-new-privileges:true" in k["security_opt"]
    assert k["read_only"] is True and set(k["tmpfs"]) == {"/tmp", "/run"}
    assert all("noexec" in v for v in k["tmpfs"].values())
    assert k["mem_limit"] == k["memswap_limit"] == "256m"  # no swap
    assert k["nano_cpus"] == 500_000_000 and k["pids_limit"] == 64
    assert k["volumes"] == {} and k["devices"] == []
    assert "network_mode" not in k and k["network"].startswith("arena-")
    assert k["ports"] == {
        "80/tcp": ("198.51.100.7", 31001)
    }  # bound to the public IP only
    assert k["runtime"] == "runsc" and k["ipc_mode"] == "private"
    assert k["restart_policy"] == {"Name": "no"}
    assert "docker.sock" not in json.dumps(k)


def test_container_kwargs_reject_excess_resources():
    cfg = make_config()
    with pytest.raises(BackendError):
        container_kwargs(spec_obj(memory_mb=999999), cfg, {})
    with pytest.raises(BackendError):
        container_kwargs(spec_obj(pids=10**6), cfg, {})


def test_network_isolation_options():
    n = network_kwargs("abc")
    assert n["options"]["com.docker.network.bridge.enable_icc"] == "false"
    assert n["labels"]["arena.managed"] == "true"


try:  # with the real SDK installed the backend expects docker.errors.NotFound
    from docker.errors import NotFound as _NotFoundBase
except ImportError:  # pragma: no cover
    _NotFoundBase = KeyError


class FakeNotFound(_NotFoundBase):
    def __init__(self, name):
        super().__init__(f"{name} not found")


class FakeContainer:
    def __init__(self, name, status="running"):
        self.name, self.status, self.removed = name, status, False

    def reload(self):
        pass

    def remove(self, force=False):
        self.removed = True

    def stop(self, timeout=5):
        self.status = "exited"

    def start(self):
        self.status = "running"


class FakeDocker:
    def __init__(self):
        self.containers = self
        self.networks = FakeNetworks()
        self.items = {}
        self.run_calls = []

    def get(self, name):
        if name not in self.items or self.items[name].removed:
            raise FakeNotFound(name)
        return self.items[name]

    def run(self, **kw):
        self.run_calls.append(kw)
        c = FakeContainer(kw["name"])
        self.items[kw["name"]] = c
        return c

    def list(self, all=True, filters=None):
        return [c for c in self.items.values() if not c.removed]


class FakeNetwork:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def remove(self):
        self.store.pop(self.name, None)


class FakeNetworks:
    def __init__(self):
        self.items = {}

    def get(self, name):
        if name not in self.items:
            raise FakeNotFound(name)
        return FakeNetwork(self.items, name)

    def create(self, **kw):
        self.items[kw["name"]] = kw


def test_docker_backend_with_fake_client():
    fake = FakeDocker()
    b = DockerBackend(make_config(), client=fake)
    s = spec_obj()
    ref = b.create(s, {80: 31000})
    assert ref == f"arena-{s.instance_id}" and len(fake.run_calls) == 1
    assert f"arena-{s.instance_id}" in fake.networks.items
    assert b.create(s, {80: 31000}) == ref and len(fake.run_calls) == 1  # idempotent
    assert b.status(ref) == "running"
    b.stop(ref)
    assert b.status(ref) == "stopped"
    assert b.managed_refs() == {ref}
    b.remove(ref)
    assert b.status(ref) == "missing" and fake.networks.items == {}
    assert b.check_image(IMG) and not b.check_image(IMG2)


def test_ad_check_endpoint_mock_and_allowlist(runner):
    app, _s, _b = runner
    body = {
        "round": 3,
        "event": 1,
        "services": [
            {"team_id": 1, "service_id": 7, "checker": "mock", "flag": "FLAG_X"},
            {"team_id": 2, "service_id": 7, "checker": "mock", "mock_status": "down"},
            {"team_id": 3, "service_id": 7, "checker": IMG2},  # not allowlisted
        ],
    }
    r = call(app, "POST", "/v1/ad/check", body)
    assert r.status_code == 200
    got = {x["team_id"]: x["status"] for x in r.get_json()["results"]}
    assert got == {1: "up", 2: "down", 3: "error"}
    assert (
        call(app, "POST", "/v1/ad/check", {"services": [{"team_id": "x"}]}).status_code
        == 400
    )
    assert app.test_client().post("/v1/ad/check", json=body).status_code == 401


def test_livez_is_the_only_unsigned_endpoint_and_leaks_nothing(runner):
    app, _s, _b = runner
    c = app.test_client()
    r = c.get("/livez")
    assert r.status_code == 200 and r.get_json() == {"status": "ok"}
    assert c.post("/livez").status_code in (401, 405)
    assert c.get("/livez/x").status_code == 401
    assert c.get("/v1/health").status_code == 401
    assert c.get("/livez?x=1").get_json() == {"status": "ok"}


def test_store_update_rejects_unknown_columns(tmp_path):
    from arena_runner.store import Store

    st = Store(str(tmp_path / "s.db"))
    st.insert("i1", "img", {}, 60)
    with pytest.raises(ValueError):
        st.update("i1", **{"status=1, id": "x"})


def test_force_user_is_validated_and_applied():
    from arena_runner.backends.docker import container_kwargs

    base = {"ARENA_RUNNER_KEYS": "k1:" + "s" * 40}
    for bad in ("0", "0:0", "root", "1000:0", "-1"):
        with pytest.raises(ValueError):
            RunnerConfig.from_env({**base, "ARENA_RUNNER_FORCE_USER": bad})
    cfg = RunnerConfig.from_env({**base, "ARENA_RUNNER_FORCE_USER": "65534:65534"})
    kw = container_kwargs(spec_obj(), cfg, {8080: 30000})
    assert kw["user"] == "65534:65534"
    cfg = RunnerConfig.from_env(base)
    assert "user" not in container_kwargs(spec_obj(), cfg, {8080: 30000})

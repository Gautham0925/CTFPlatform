"""Real-container isolation tests for the Docker backend.

Run on a runner host (or a Docker-capable CI runner):
    ARENA_RUNNER_DOCKER_TESTS=1 ARENA_TEST_IMAGE=<repo@sha256:...> pytest runner/tests
The image must serve HTTP on 8080 (runner/tests/images/web).
These tests modify iptables (ARENA-EGRESS / ARENA-INPUT chains).
"""

import os
import socket
import subprocess
import threading
import time
import urllib.request
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ARENA_RUNNER_DOCKER_TESTS") != "1"
    or not os.environ.get("ARENA_TEST_IMAGE"),
    reason="set ARENA_RUNNER_DOCKER_TESTS=1 and ARENA_TEST_IMAGE on a Docker host",
)

IMAGE = os.environ.get("ARENA_TEST_IMAGE", "")
FIREWALL = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "firewall", "docker-user.sh"
)


@pytest.fixture(scope="module")
def backend():
    from arena_runner.backends.docker import DockerBackend
    from arena_runner.config import RunnerConfig

    cfg = RunnerConfig(
        keys={"k": "x" * 40},
        backend="docker",
        allowed_images=frozenset({IMAGE}),
        public_host="127.0.0.1",
        port_min=38100,
        port_max=38199,
        db_path=":memory:",
    )
    return DockerBackend(cfg)


@pytest.fixture
def instance(backend):
    from arena_runner.backends.base import InstanceSpec

    spec = InstanceSpec(
        instance_id=str(uuid.uuid4()),
        image=IMAGE,
        ports=[8080],
        cpu_millis=250,
        memory_mb=64,
        pids=32,
        ttl_seconds=600,
        env={"ARENA_FLAG": "flag{t}"},
        labels={"event": "1"},
    )
    port = 38100 + (uuid.uuid4().int % 100)
    ref = backend.create(spec, {8080: port})
    container = backend.client.containers.get(ref)
    for _ in range(50):
        container.reload()
        if container.status == "running":
            break
        time.sleep(0.1)
    yield backend, ref, container, port
    backend.remove(ref)


def sh(container, cmd):
    res = container.exec_run(["sh", "-c", cmd])
    return res.exit_code, res.output.decode(errors="replace")


def test_container_runtime_hardening(instance):
    _b, _ref, c, _port = instance
    hc = c.attrs["HostConfig"]
    assert hc["Privileged"] is False
    assert hc["CapDrop"] == ["ALL"] and not hc.get("CapAdd")
    assert "no-new-privileges:true" in hc["SecurityOpt"]
    assert hc["ReadonlyRootfs"] is True
    assert hc["Memory"] == 64 * 1024 * 1024 and hc["MemorySwap"] == 64 * 1024 * 1024
    assert hc["PidsLimit"] == 32 and hc["NanoCpus"] == 250_000_000
    assert not hc.get("Binds") and not c.attrs.get("Mounts")
    assert hc["IpcMode"] == "private"


def test_no_capabilities_inside(instance):
    _b, _ref, c, _port = instance
    code, out = sh(c, "grep -E 'Cap(Eff|Prm|Bnd)' /proc/self/status")
    assert code == 0
    for line in out.strip().splitlines():
        assert line.split()[1] == "0000000000000000", line


def test_filesystem_restrictions(instance):
    _b, _ref, c, _port = instance
    assert sh(c, "touch /evil")[0] != 0  # read-only rootfs
    assert sh(c, "echo x > /tmp/ok && cat /tmp/ok")[0] == 0  # tmpfs writable
    assert sh(c, "cp /bin/busybox /tmp/bb && /tmp/bb true")[0] != 0  # noexec tmpfs
    assert sh(c, "ls /var/run/docker.sock")[0] != 0  # no Docker socket
    assert sh(c, "mount -t proc proc /mnt")[0] != 0  # no mount (no CAP_SYS_ADMIN)


def test_pids_limit_enforced(instance):
    _b, _ref, c, _port = instance
    code, out = sh(c, "for i in $(seq 1 60); do sleep 30 & done 2>&1; echo spawned")
    assert (
        "Resource temporarily unavailable" in out or "can't fork" in out or code != 0
    ), out


def test_published_port_reachable(instance):
    _b, _ref, _c, port = instance
    body = None
    for _ in range(30):
        try:
            body = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).read()
            break
        except OSError:
            time.sleep(0.2)
    assert body and b"challenge-ok" in body


def _gateway(c):
    nets = c.attrs["NetworkSettings"]["Networks"]
    return next(iter(nets.values()))["Gateway"]


def _can_connect(c, ip, port):
    return (
        sh(c, f"echo x | nc -w 2 {ip} {port} >/dev/null 2>&1; echo $?")[1]
        .strip()
        .endswith("0")
    )


@pytest.fixture
def host_listener():
    """A TCP service on the host standing in for CTFd/DB/Redis."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", 0))
    srv.listen(16)
    stop = threading.Event()

    def accept():
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conn.close()
            except OSError:
                pass

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield srv.getsockname()[1]
    stop.set()
    srv.close()


def test_firewall_blocks_host_and_internal_services(instance, host_listener):
    _b, _ref, c, _port = instance
    gw = _gateway(c)
    subprocess.run(["sh", FIREWALL, "--remove"], check=False, capture_output=True)
    baseline = _can_connect(c, gw, host_listener)
    proc = subprocess.run(
        ["sh", FIREWALL],
        capture_output=True,
        text=True,
        env=dict(os.environ, ARENA_CONTAINER_NETS="172.16.0.0/12"),
    )
    assert proc.returncode == 0, proc.stderr
    try:
        assert baseline is True, "precondition: host service reachable without firewall"
        assert _can_connect(c, gw, host_listener) is False  # platform service on host
        assert _can_connect(c, gw, 6379) is False  # Redis on host
        assert _can_connect(c, "169.254.169.254", 80) is False  # cloud metadata
        # Inbound players still reach the challenge (established/related allowed).
        assert urllib.request.urlopen(f"http://127.0.0.1:{_port}/", timeout=3).read()
    finally:
        subprocess.run(["sh", FIREWALL, "--remove"], check=False, capture_output=True)

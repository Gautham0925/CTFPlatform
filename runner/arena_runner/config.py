"""Runner configuration from environment variables (no secrets in code)."""

import os
import re
from dataclasses import dataclass, field

DIGEST_REF = re.compile(r"^[a-z0-9][a-z0-9._/:-]{0,200}@sha256:[0-9a-f]{64}$")


def _keys(raw):
    keys = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        key_id, _, secret = part.partition(":")
        if not key_id or len(secret) < 32:
            raise ValueError(
                "ARENA_RUNNER_KEYS entries must be key_id:secret (secret >= 32 chars)"
            )
        keys[key_id] = secret
    return keys


def _user(raw):
    raw = (raw or "").strip()
    if raw and not re.fullmatch(r"[1-9][0-9]{0,9}(:[1-9][0-9]{0,9})?", raw):
        raise ValueError("ARENA_RUNNER_FORCE_USER must be a non-root uid[:gid]")
    return raw


@dataclass
class RunnerConfig:
    keys: dict
    backend: str = "mock"
    allowed_images: frozenset = field(default_factory=frozenset)
    public_host: str = "127.0.0.1"
    port_min: int = 30000
    port_max: int = 30999
    max_instances: int = 200
    max_ttl_seconds: int = 4 * 3600
    max_memory_mb: int = 2048
    max_cpu_millis: int = 2000
    max_pids: int = 512
    db_path: str = "/var/lib/arena-runner/state.db"
    max_skew: int = 60
    reap_interval: int = 15
    docker_runtime: str = ""  # e.g. "runsc" for gVisor
    # Run every challenge container as this user ("uid:gid"), overriding the
    # image's USER. Empty = image default (may be root inside the container).
    force_user: str = ""
    # Interface that published challenge ports bind to. Set this to the
    # runner's public IP so challenges are never exposed on internal NICs.
    bind_ip: str = "0.0.0.0"  # noqa: S104 - operator-configurable, see docs

    @classmethod
    def from_env(cls, env=None):
        env = env if env is not None else os.environ
        keys = _keys(env.get("ARENA_RUNNER_KEYS", ""))
        if not keys:
            raise ValueError("ARENA_RUNNER_KEYS is required")
        images = frozenset(
            i.strip()
            for i in env.get("ARENA_RUNNER_ALLOWED_IMAGES", "").split(",")
            if i.strip()
        )
        for ref in images:
            if not DIGEST_REF.match(ref):
                raise ValueError(f"allowed image must be pinned by digest: {ref}")
        lo, _, hi = env.get("ARENA_RUNNER_PORT_RANGE", "30000-30999").partition("-")
        return cls(
            keys=keys,
            backend=env.get("ARENA_RUNNER_BACKEND", "mock"),
            allowed_images=images,
            public_host=env.get("ARENA_RUNNER_PUBLIC_HOST", "127.0.0.1"),
            port_min=int(lo),
            port_max=int(hi),
            max_instances=int(env.get("ARENA_RUNNER_MAX_INSTANCES", 200)),
            max_ttl_seconds=int(env.get("ARENA_RUNNER_MAX_TTL", 4 * 3600)),
            db_path=env.get("ARENA_RUNNER_DB", "/var/lib/arena-runner/state.db"),
            docker_runtime=env.get("ARENA_RUNNER_DOCKER_RUNTIME", ""),
            force_user=_user(env.get("ARENA_RUNNER_FORCE_USER", "")),
            bind_ip=env.get("ARENA_RUNNER_BIND_IP", "0.0.0.0"),  # noqa: S104
        )

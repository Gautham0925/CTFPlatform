"""Backend interface."""

from dataclasses import dataclass, field


@dataclass
class InstanceSpec:
    instance_id: str
    image: str
    ports: list  # container ports to expose, e.g. [80]
    cpu_millis: int
    memory_mb: int
    pids: int
    ttl_seconds: int
    env: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)


class BackendError(Exception):
    pass


class Backend:
    name = "base"

    def create(self, spec, host_ports):
        """Create + start. Returns backend_ref. Must be idempotent on spec.instance_id."""
        raise NotImplementedError

    def status(self, ref):
        """'running' | 'stopped' | 'missing'"""
        raise NotImplementedError

    def stop(self, ref):
        raise NotImplementedError

    def start(self, ref):
        raise NotImplementedError

    def remove(self, ref):
        """Remove container + its network. Idempotent."""
        raise NotImplementedError

    def managed_refs(self):
        """All refs this backend manages (for orphan cleanup)."""
        raise NotImplementedError

    def stats(self, ref):
        return {}

    def check_image(self, image):
        return True

    def run_checker(self, image, env, timeout):
        """Run a checker; returns its exit code (0 up, 1 mumble, 2 corrupt, 3 down)."""
        raise NotImplementedError

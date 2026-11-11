"""In-memory backend for development and tests (no Docker required)."""

import threading

from arena_runner.backends.base import Backend, BackendError


class MockBackend(Backend):
    name = "mock"

    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self.containers = {}  # ref -> dict
        self.fail_next_create = False

    def create(self, spec, host_ports):
        with self._lock:
            if self.fail_next_create:
                self.fail_next_create = False
                raise BackendError("simulated create failure")
            ref = f"mock-{spec.instance_id}"
            self.containers.setdefault(
                ref, {"spec": spec, "status": "running", "ports": host_ports}
            )
            return ref

    def status(self, ref):
        c = self.containers.get(ref)
        return c["status"] if c else "missing"

    def stop(self, ref):
        if ref in self.containers:
            self.containers[ref]["status"] = "stopped"

    def start(self, ref):
        if ref not in self.containers:
            raise BackendError("missing container")
        self.containers[ref]["status"] = "running"

    def remove(self, ref):
        self.containers.pop(ref, None)

    def managed_refs(self):
        return set(self.containers)

    def run_checker(self, image, env, timeout):
        return 0

    def stats(self, ref):
        c = self.containers.get(ref)
        if not c:
            return {}
        return {
            "cpu_percent": 0.0,
            "memory_mb": 1,
            "memory_limit_mb": c["spec"].memory_mb,
        }

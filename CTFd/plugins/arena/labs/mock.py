"""Offline mock provider (development/tests). State is process-local."""

import threading
import time
import uuid

from CTFd.plugins.arena.labs.base import LabProvider, ProviderError

LABS = [
    {
        "id": "linux-101",
        "name": "Linux Fundamentals",
        "description": "Shell basics on a disposable VM.",
    },
    {
        "id": "web-201",
        "name": "Web Exploitation",
        "description": "A vulnerable web application.",
    },
    {
        "id": "ad-301",
        "name": "Active Directory",
        "description": "A small Windows domain.",
    },
]


class MockProvider(LabProvider):
    name = "mock"
    _sessions = {}
    _lock = threading.Lock()

    def __init__(self, config):
        self.config = config
        self.fail = bool(config.get("simulate_failure"))

    def _guard(self):
        if self.fail:
            raise ProviderError("simulated provider failure")

    def health(self):
        self._guard()
        return {"status": "ok", "provider": "mock"}

    def list_labs(self):
        self._guard()
        return [dict(lab) for lab in LABS]

    def create(self, template_id, session_id, ttl_seconds, owner):
        self._guard()
        if template_id not in {lab["id"] for lab in LABS}:
            raise ProviderError("unknown template")
        with self._lock:
            for ext_id, s in self._sessions.items():
                if s["session_id"] == session_id:  # idempotent
                    return {"id": ext_id, "status": s["status"]}
            ext_id = "mock-" + uuid.uuid4().hex[:12]
            self._sessions[ext_id] = {
                "session_id": session_id,
                "template": template_id,
                "status": "ready",
                "completed": False,
                "expires": time.time() + ttl_seconds,
            }
        return {"id": ext_id, "status": "ready"}

    def status(self, external_id):
        self._guard()
        s = self._sessions.get(external_id)
        if s is None:
            return {"status": "expired", "completed": False}
        return {"status": s["status"], "completed": s["completed"]}

    def connection(self, external_id):
        self._guard()
        if external_id not in self._sessions:
            raise ProviderError("unknown session")
        return {
            "type": "ssh",
            "host": "lab.example.test",
            "port": "2222",
            "username": "player",
            "password": "mock-" + external_id[-6:],
        }

    def reset(self, external_id):
        self._guard()
        if external_id in self._sessions:
            self._sessions[external_id]["completed"] = False

    def release(self, external_id):
        self._sessions.pop(external_id, None)

    # --- test/demo helper (provider-side event, never callable by clients)
    @classmethod
    def mark_completed(cls, external_id):
        cls._sessions[external_id]["completed"] = True

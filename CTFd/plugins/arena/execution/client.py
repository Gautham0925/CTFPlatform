"""Clients for the challenge runner.

* MockRunnerClient — default (ARENA_RUNNER_MODE=mock): no network, no Docker.
  Stateless, so it behaves identically across gunicorn workers; endpoints are
  derived deterministically from the instance UUID. Development/tests only.
* HTTPRunnerClient — ARENA_RUNNER_MODE=remote: talks to runner/ over HTTPS
  with HMAC-signed requests (execution/signing.py), timeouts and bounded
  retries. Create is idempotent on the instance UUID, so retries are safe.
"""

import hashlib
import json
import logging
import time

import requests
from flask import current_app

from CTFd.plugins.arena.execution import signing

log = logging.getLogger("arena.runner_client")


class RunnerError(Exception):
    pass


class RunnerUnavailable(RunnerError):
    pass


class MockRunnerClient:
    mode = "mock"
    _deleted = set()  # process-local: only affects status of deleted ids

    def health(self):
        return {"status": "ok", "backend": "mock-client"}

    def check_image(self, reference):
        return {"image": reference, "allowed": True}

    def _port(self, instance_id, container_port):
        h = int(
            hashlib.sha256(f"{instance_id}:{container_port}".encode()).hexdigest(), 16
        )
        return 40000 + h % 20000

    def create(self, spec):
        ports = {
            str(p): self._port(spec["instance_id"], p) for p in spec.get("ports", [])
        }
        return {
            "instance_id": spec["instance_id"],
            "status": "running",
            "host": "127.0.0.1",
            "ports": ports,
            "expires_at": int(time.time()) + int(spec.get("ttl_seconds", 3600)),
        }

    def get(self, instance_id):
        if instance_id in self._deleted:
            return {"instance_id": instance_id, "status": "removed"}
        return {"instance_id": instance_id, "status": "running"}

    def stop(self, instance_id):
        return {"instance_id": instance_id, "status": "stopped"}

    def start(self, instance_id):
        return {"instance_id": instance_id, "status": "running"}

    def delete(self, instance_id):
        self._deleted.add(instance_id)
        return {"instance_id": instance_id, "status": "removed"}

    def ad_check(self, payload):
        """Mock checker for Attack & Defense (Phase 9): deterministic
        results; flags are considered planted."""
        out = []
        for item in payload.get("services", []):
            status = item.get("mock_status") or "up"
            out.append(
                {
                    "team_id": item["team_id"],
                    "service_id": item["service_id"],
                    "status": status,
                    "detail": "mock",
                }
            )
        return {"round": payload.get("round"), "results": out}


class HTTPRunnerClient:
    mode = "remote"

    def __init__(
        self, base_url, key_id, secret, timeout=(3, 10), verify=True, retries=2
    ):
        self.base_url = base_url.rstrip("/")
        self.key_id = key_id
        self.secret = secret
        self.timeout = timeout
        self.verify = verify
        self.retries = retries
        self.session = requests.Session()

    def _request(self, method, path, body=None, retry=True):
        raw = (
            json.dumps(body, separators=(",", ":")).encode()
            if body is not None
            else b""
        )
        attempts = self.retries + 1 if retry else 1
        last = None
        for i in range(attempts):
            headers = signing.sign(self.secret, method, path, raw, key_id=self.key_id)
            headers["Content-Type"] = "application/json"
            try:
                r = self.session.request(
                    method,
                    self.base_url + path,
                    data=raw,
                    headers=headers,
                    timeout=self.timeout,
                    verify=self.verify,
                    allow_redirects=False,
                )
            except requests.RequestException as e:
                last = e
                time.sleep(0.2 * (i + 1))
                continue
            if r.status_code >= 500 and r.status_code != 502 and i < attempts - 1:
                last = RunnerError(f"runner returned {r.status_code}")
                time.sleep(0.2 * (i + 1))
                continue
            try:
                data = r.json()
            except ValueError:
                raise RunnerError(f"runner returned non-JSON ({r.status_code})")
            if r.status_code == 401:
                raise RunnerError("runner rejected our signature (check keys/clock)")
            if r.status_code >= 400 and r.status_code != 502:
                raise RunnerError(data.get("error") or f"runner error {r.status_code}")
            return data
        raise RunnerUnavailable(f"runner unreachable: {type(last).__name__}")

    def health(self):
        return self._request("GET", "/v1/health")

    def check_image(self, reference):
        return self._request("POST", "/v1/images/check", {"image": reference})

    def create(self, spec):
        return self._request("POST", "/v1/instances", spec)  # idempotent on instance_id

    def get(self, instance_id):
        return self._request("GET", f"/v1/instances/{instance_id}")

    def stop(self, instance_id):
        return self._request("POST", f"/v1/instances/{instance_id}/stop")

    def start(self, instance_id):
        return self._request("POST", f"/v1/instances/{instance_id}/start")

    def delete(self, instance_id):
        return self._request("DELETE", f"/v1/instances/{instance_id}")

    def ad_check(self, payload):
        return self._request("POST", "/v1/ad/check", payload, retry=False)


_override = {"client": None}


def set_client(client):
    """Tests / embedding: force a specific client."""
    _override["client"] = client


def get_client():
    if _override["client"] is not None:
        return _override["client"]
    settings = current_app.extensions["arena"]["settings"]
    if settings.runner_mode == "mock":
        return MockRunnerClient()
    cfg = current_app.config
    url = cfg.get("ARENA_RUNNER_URL") or ""
    import os

    url = url or os.environ.get("ARENA_RUNNER_URL", "")
    key_id = cfg.get("ARENA_RUNNER_KEY_ID") or os.environ.get("ARENA_RUNNER_KEY_ID", "")
    secret = cfg.get("ARENA_RUNNER_SECRET") or os.environ.get("ARENA_RUNNER_SECRET", "")
    allow_http = str(
        cfg.get("ARENA_RUNNER_ALLOW_HTTP")
        or os.environ.get("ARENA_RUNNER_ALLOW_HTTP", "")
    ).lower() in ("1", "true")
    if not url or not key_id or len(secret) < 32:
        raise RunnerError(
            "remote runner is not configured (ARENA_RUNNER_URL/KEY_ID/SECRET)"
        )
    if not url.startswith("https://") and not allow_http:
        raise RunnerError(
            "ARENA_RUNNER_URL must use https:// (set ARENA_RUNNER_ALLOW_HTTP for local tests)"
        )
    ca = cfg.get("ARENA_RUNNER_CA") or os.environ.get("ARENA_RUNNER_CA") or True
    return HTTPRunnerClient(url, key_id, secret, verify=ca)

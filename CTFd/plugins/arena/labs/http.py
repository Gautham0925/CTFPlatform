"""Generic HTTP provider implementing the documented REST contract
(docs/API_DOCUMENTATION.md#external-lab-provider-contract).

    GET    {base}/health
    GET    {base}/labs
    POST   {base}/sessions                 {template_id, session_id, ttl_seconds, owner}
    GET    {base}/sessions/{id}
    GET    {base}/sessions/{id}/connection
    POST   {base}/sessions/{id}/reset
    DELETE {base}/sessions/{id}
Authorization: Bearer <token> (credentials {"token": ...}).
"""

import json
import logging
from urllib.parse import quote

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from CTFd.plugins.arena.labs.base import (
    LabProvider,
    ProviderError,
    ProviderTimeout,
    clean_connection,
    clean_labs,
    clean_status,
)
from CTFd.plugins.arena.labs.ssrf import SSRFError, check_url

log = logging.getLogger("arena.labs.http")
MAX_RESPONSE_BYTES = 1024 * 1024


class _Retryable(ProviderError):
    pass


class GenericHTTPProvider(LabProvider):
    name = "generic_http"

    def __init__(self, base_url, credentials, config):
        self.base_url = (base_url or "").rstrip("/")
        self.token = (credentials or {}).get("token")
        self.timeout = (
            float(config.get("connect_timeout", 3)),
            float(config.get("read_timeout", 10)),
        )
        self.attempts = int(config.get("retries", 2)) + 1

    def _call(self, method, path, body=None, idempotent=True):
        url = self.base_url + path
        try:
            check_url(
                url
            )  # re-check at request time (narrows, not closes, DNS rebinding)
        except SSRFError as e:
            raise ProviderError(f"blocked: {e}")

        @retry(
            retry=retry_if_exception_type(_Retryable),
            stop=stop_after_attempt(self.attempts if idempotent else 1),
            wait=wait_exponential(multiplier=0.2, max=2),
            reraise=True,
        )
        def do():
            headers = {"Accept": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            try:
                r = requests.request(
                    method,
                    url,
                    json=body,
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.Timeout:
                raise _Retryable("timeout")
            except requests.RequestException as e:
                raise _Retryable(type(e).__name__)
            raw = r.raw.read(MAX_RESPONSE_BYTES + 1, decode_content=True)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProviderError("provider response too large")
            if r.status_code >= 500 or r.status_code == 429:
                raise _Retryable(f"status {r.status_code}")
            if 300 <= r.status_code < 400:
                raise ProviderError("redirects are not followed")
            if r.status_code == 404:
                return {"_not_found": True}
            if r.status_code >= 400:
                raise ProviderError(f"provider rejected request ({r.status_code})")
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except ValueError:
                raise ProviderError("provider returned invalid JSON")

        try:
            return do()
        except _Retryable as e:
            if "timeout" in str(e):
                raise ProviderTimeout("provider timed out")
            raise ProviderError(f"provider unavailable: {e}")

    def health(self):
        data = self._call("GET", "/health")
        return {"status": str(data.get("status", "unknown"))[:32]}

    def list_labs(self):
        return clean_labs(self._call("GET", "/labs"))

    def create(self, template_id, session_id, ttl_seconds, owner):
        data = self._call(
            "POST",
            "/sessions",
            {
                "template_id": template_id,
                "session_id": session_id,
                "ttl_seconds": int(ttl_seconds),
                "owner": owner,
            },
        )
        if not isinstance(data.get("id"), str) or not data["id"]:
            raise ProviderError("provider did not return a session id")
        return {"id": data["id"][:128], "status": clean_status(data)["status"]}

    def status(self, external_id):
        data = self._call("GET", f"/sessions/{quote(external_id, safe='')}")
        if data.get("_not_found"):
            return {"status": "expired", "completed": False}
        return clean_status(data)

    def connection(self, external_id):
        return clean_connection(
            self._call("GET", f"/sessions/{quote(external_id, safe='')}/connection")
        )

    def reset(self, external_id):
        self._call(
            "POST", f"/sessions/{quote(external_id, safe='')}/reset", idempotent=False
        )

    def release(self, external_id):
        self._call("DELETE", f"/sessions/{quote(external_id, safe='')}")

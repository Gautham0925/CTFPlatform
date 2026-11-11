"""Lab provider contract.

Every method may raise ProviderError (any failure) or ProviderTimeout. All
returned data is UNTRUSTED and is validated by the caller.

    health()                         -> {"status": "ok"|..., ...}
    list_labs()                      -> [{"id", "name", "description"}]
    create(template_id, session_id, ttl_seconds, owner)
                                     -> {"id": external_id, "status": ...}
                                        (idempotent on session_id)
    status(external_id)              -> {"status": provisioning|ready|stopped|expired|failed,
                                         "completed": bool}
    connection(external_id)          -> {"type": "ssh"|"url"|"vpn", host/port/url/username/password}
    reset(external_id)               -> None
    release(external_id)             -> None   (idempotent)
"""

PROVIDER_STATUSES = ("provisioning", "ready", "stopped", "expired", "failed")
CONNECTION_FIELDS = {
    "type": 16,
    "host": 255,
    "port": 8,
    "url": 500,
    "username": 128,
    "password": 256,
    "note": 500,
}


class ProviderError(Exception):
    pass


class ProviderTimeout(ProviderError):
    pass


class LabProvider:
    name = "base"

    def health(self):
        raise NotImplementedError

    def list_labs(self):
        raise NotImplementedError

    def create(self, template_id, session_id, ttl_seconds, owner):
        raise NotImplementedError

    def status(self, external_id):
        raise NotImplementedError

    def connection(self, external_id):
        raise NotImplementedError

    def reset(self, external_id):
        raise NotImplementedError

    def release(self, external_id):
        raise NotImplementedError


def clean_status(data):
    if not isinstance(data, dict):
        raise ProviderError("malformed status response")
    status = data.get("status")
    if status not in PROVIDER_STATUSES:
        raise ProviderError("unknown status from provider")
    return {"status": status, "completed": data.get("completed") is True}


def clean_connection(data):
    if not isinstance(data, dict):
        raise ProviderError("malformed connection response")
    out = {}
    for key, max_len in CONNECTION_FIELDS.items():
        if data.get(key) is not None:
            out[key] = str(data[key])[:max_len]
    if out.get("url") and not out["url"].startswith(
        ("https://", "http://", "ssh://", "rdp://")
    ):
        out.pop("url")  # never pass javascript:/data: URLs to participants
    return out


def clean_labs(data):
    labs = data.get("labs") if isinstance(data, dict) else None
    if not isinstance(labs, list):
        raise ProviderError("malformed lab list")
    out = []
    for lab in labs[:500]:
        if isinstance(lab, dict) and isinstance(lab.get("id"), str):
            out.append(
                {
                    "id": lab["id"][:128],
                    "name": str(lab.get("name") or lab["id"])[:128],
                    "description": str(lab.get("description") or "")[:2000],
                }
            )
    return out

"""SSRF protection for outbound provider requests.

* scheme must be https (http only for hosts in ARENA_LAB_ALLOWED_PRIVATE_HOSTS)
* every resolved address must be public, unless the host is explicitly
  allowlisted (ARENA_LAB_ALLOWED_PRIVATE_HOSTS, comma separated)
* checked when a provider is configured AND before every request, which
  stops a provider host that is re-pointed after configuration. Residual
  risk: the HTTP client resolves the name again after the check, so a DNS
  server answering differently within that window is not prevented (see
  docs/SECURITY_GUIDE.md). Redirects are never followed.
"""

import ipaddress
import socket
from urllib.parse import urlparse

from flask import current_app


class SSRFError(ValueError):
    pass


def _allowed_private():
    raw = current_app.config.get("ARENA_LAB_ALLOWED_PRIVATE_HOSTS") or ""
    return {h.strip().lower() for h in str(raw).split(",") if h.strip()}


def check_url(url):
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if not host or parsed.username or parsed.password:
        raise SSRFError("URL must have a host and no embedded credentials")
    allow = host in _allowed_private()
    if parsed.scheme != "https" and not (allow and parsed.scheme == "http"):
        raise SSRFError("provider URL must use https://")
    if allow:
        return url
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise SSRFError("provider host does not resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise SSRFError("provider host resolves to a non-public address")
    return url

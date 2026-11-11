"""Black-box security probes against a RUNNING Arena deployment.

    python tests/arena/security/live_probe.py https://127.0.0.1 arena.test

Needs the load-test seed (tests/arena/load/seed.py) and the admin account
admin / AdminPass-123! (test stacks only). Prints one PASS/FAIL line per probe
and exits non-zero if any probe fails. Never point it at a production site
you do not own.
"""

import re
import socket
import ssl
import sys

import requests
import urllib3

urllib3.disable_warnings()
BASE, HOST = sys.argv[1], sys.argv[2]
API = BASE + "/api/v1/arena"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def session(user=None, pw=None):
    s = requests.Session()
    s.trust_env = False
    s.verify = False
    s.headers["Host"] = HOST
    if user:
        n = re.search(
            r"csrfNonce': \"([0-9a-f]+)\"", s.get(BASE + "/login").text
        ).group(1)
        s.post(BASE + "/login", data={"name": user, "password": pw, "nonce": n})
        s.headers["CSRF-Token"] = re.search(
            r"csrfNonce': \"([0-9a-f]+)\"", s.get(BASE + "/challenges").text
        ).group(1)
    return s


def tls_version_accepted(version):
    import warnings

    warnings.filterwarnings("ignore", category=DeprecationWarning)  # TLS1.0/1.1 enums
    host = BASE.split("://")[1].split(":")[0]
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ctx.maximum_version = version
    try:
        with socket.create_connection((host, 443), timeout=5) as sock:
            with ctx.wrap_socket(sock, server_hostname=HOST):
                return True
    except (ssl.SSLError, OSError):
        return False


# --- transport --------------------------------------------------------------
check("TLS 1.0 refused", not tls_version_accepted(ssl.TLSVersion.TLSv1))
check("TLS 1.1 refused", not tls_version_accepted(ssl.TLSVersion.TLSv1_1))
check("TLS 1.2 accepted", tls_version_accepted(ssl.TLSVersion.TLSv1_2))
check("TLS 1.3 accepted", tls_version_accepted(ssl.TLSVersion.TLSv1_3))
anon = session()
r = anon.get(BASE.replace("https", "http") + "/arena", allow_redirects=False)
check(
    "HTTP redirects to HTTPS",
    r.status_code == 301 and r.headers.get("Location", "").startswith("https://"),
)
r = anon.get(BASE + "/arena")
check("HSTS header", "max-age=" in r.headers.get("Strict-Transport-Security", ""))
check("nosniff header", r.headers.get("X-Content-Type-Options") == "nosniff")
check(
    "no nginx version leak",
    r.headers.get("Server", "") == "nginx",
    r.headers.get("Server"),
)
r = anon.get(BASE + "/arena/readyz")
check(
    "readyz exposes booleans only",
    set(r.json()) == {"ready", "checks"}
    and all(isinstance(v, bool) for v in r.json()["checks"].values()),
)

# --- authentication / CSRF ------------------------------------------------
evs = {
    e["slug"]: e["id"] for e in anon.get(API + "/events?per_page=100").json()["data"]
}
a, c_ = evs["load-a"], evs["load-c"]
p = session("loaduser010", "LoadPass-123!")  # registered in load-a and load-b only
chs = p.get(f"{API}/events/{a}/challenges").json()["data"]
ch = chs[0]["id"]
nocsrf = requests.Session()
nocsrf.trust_env = False
nocsrf.verify = False
nocsrf.headers["Host"] = HOST
nocsrf.cookies = p.cookies
r = nocsrf.post(f"{API}/events/{a}/challenges/{ch}/attempt", json={"submission": "x"})
check("JSON POST without CSRF token rejected", r.status_code == 403, r.status_code)
r = p.post(f"{API}/events/{a}/challenges/{ch}/attempt", data={"submission": "x"})
check(
    "form-encoded POST to Arena API rejected",
    r.status_code in (400, 403, 415),
    r.status_code,
)
r = anon.post(f"{API}/events/{a}/challenges/{ch}/attempt", json={"submission": "x"})
check("anonymous submission rejected", r.status_code in (401, 403), r.status_code)

# --- authorization / IDOR -------------------------------------------------
r = p.get(f"{API}/events/{c_}/challenges")
check(
    "non-participant cannot list another event's challenges",
    r.status_code in (403, 404),
    r.status_code,
)
r = p.get(f"{API}/events/{evs['load-b']}/challenges/{ch}")
check(
    "challenge id from event A via event B URL -> 404",
    r.status_code == 404,
    r.status_code,
)
r = p.get(f"{API}/admin/events/{a}")
check("participant cannot read admin event", r.status_code in (401, 403), r.status_code)
r = p.get(BASE + "/api/v1/challenges")
names = {x.get("name", "") for x in (r.json().get("data") or [])} if r.ok else set()
check(
    "event challenges invisible in native /api/v1/challenges",
    not any(n.startswith("load-") for n in names),
    f"{len(names)} native visible",
)
r = p.get(f"{BASE}/api/v1/challenges/{ch}")
check(
    "native challenge endpoint does not reveal event challenge",
    r.status_code in (403, 404),
    r.status_code,
)

# --- injection / XSS / traversal -----------------------------------------
r = anon.get(API + "/events/by-slug/' OR 1=1--")
check("SQL-ish slug -> 404, no 500", r.status_code == 404, r.status_code)
r = anon.get(API + "/events?status=%27%20OR%201%3D1--&page=1")
check("injection in query param -> no 500", r.status_code < 500, r.status_code)
r = anon.get(BASE + "/plugins/arena/assets/../../../../etc/passwd")
check(
    "asset path traversal blocked",
    r.status_code in (400, 404) and "root:" not in r.text,
    r.status_code,
)
admin = session("admin", "AdminPass-123!")
r = admin.post(
    API + "/admin/events",
    json={
        "slug": "xss-probe-" + str(abs(hash(BASE)) % 10000),
        "name": "XSS <img src=x onerror=alert(1)>",
        "type": "jeopardy",
        "description": "<script>alert(1)</script>\n\n**bold** [x](javascript:alert(1))"
        " <img src=x onerror=alert(2)>",
    },
)
if r.status_code in (200, 201):
    ev = r.json()["data"]
    html = ev.get("description_html", "")
    check(
        "Markdown sanitizer: no <script>, javascript: href or event handler",
        "<script" not in html
        and 'href="javascript:' not in html
        and "onerror" not in html
        and "<strong>bold</strong>" in html,
        html[:120],
    )
    admin.delete(f"{API}/admin/events/{ev['id']}", json={})
else:
    check("XSS probe event created", False, r.text[:120])
r = p.post(
    f"{API}/events/{a}/challenges/{ch}/attempt",
    json={"submission": "A" * (2 * 1024 * 1024)},
)
check("oversized submission rejected", r.status_code in (400, 413), r.status_code)

print(f"\n{sum(results)}/{len(results)} probes passed")
sys.exit(0 if all(results) else 1)

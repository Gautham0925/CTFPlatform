"""Starts a live Arena server, seeds data and runs ui.e2e.js with Playwright.

    TESTING_DATABASE_URL=mysql+pymysql://... python tests/arena/e2e/run_e2e.py <screenshot_dir>
Requires a server database (the HTTP server runs in another thread).
"""

import json
import os
import subprocess
import sys
import threading

from werkzeug.serving import make_server

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
)

from tests.arena.helpers import create_arena_ctfd, gen_event, make_user  # noqa: E402
from tests.helpers import destroy_ctfd, login_as_user  # noqa: E402


def main(shots):
    app = create_arena_ctfd()
    app.config["RATELIMIT_LOGIN"] = (1000, 5)
    app.config["SERVER_NAME"] = None
    try:
        with app.app_context():
            ev = gen_event(
                "spring-ctf",
                name="Spring CTF",
                registration_open=True,
                description="Welcome! <script>window.x=1</script>",
            )
            gen_event("hidden-draft", status="draft")
            make_user("player1")
            eid, slug = ev.id, ev.slug
        admin = login_as_user(app, name="admin")
        for name, flag in (("Warmup", "flag{e2e}"), ("Second", "flag{two}")):
            r = admin.post(
                f"/api/v1/arena/admin/events/{eid}/challenges",
                json={
                    "name": name,
                    "points": 100,
                    "visible": True,
                    "category": "Web",
                    "description": "Find it <img src=x onerror=alert(1)>",
                    "flags": [{"content": flag}],
                },
            )
            assert r.status_code == 201, r.get_json()
        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        os.makedirs(shots, exist_ok=True)
        out = subprocess.run(
            [
                "node",
                os.path.join(os.path.dirname(__file__), "ui.e2e.js"),
                base,
                slug,
                str(eid),
                shots,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        server.shutdown()
        lines = [ln for ln in out.stdout.splitlines() if ln.startswith("[")]
        if not lines:
            print(out.stdout, out.stderr)
            return 2
        results = json.loads(lines[-1])
        for r in results:
            print(
                ("PASS " if r["ok"] else "FAIL ")
                + r["name"]
                + (f"  [{r['detail']}]" if r["detail"] and not r["ok"] else "")
            )
        return 0 if all(r["ok"] for r in results) else 1
    finally:
        destroy_ctfd(app)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/arena-e2e"))

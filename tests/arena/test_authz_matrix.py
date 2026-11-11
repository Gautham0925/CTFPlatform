"""Dynamic authorization sweep over EVERY Arena route.

Calls each route as (1) an anonymous visitor and (2) a logged-in user with no
registration and no Arena role, against a public active event with data. Any
2xx answer must be on an explicit allow-list; a new route that forgets its
guard fails this test.
"""

import re

import pytest

from CTFd.config import TestingConfig
from tests.arena.helpers import create_arena_ctfd, gen_event, make_user
from tests.helpers import destroy_ctfd, login_as_user

# (method, rule) pairs that may succeed for an anonymous visitor
ANON_OK = {
    ("GET", "/arena"),
    ("GET", "/arena/e/<slug>"),
    ("GET", "/arena/e/<slug>/<section>"),  # only public sections; others 302/404
    ("GET", "/arena/healthz"),
    ("GET", "/arena/readyz"),
    ("GET", "/arena/join"),
    ("GET", "/arena/verify/<public_id>"),
    ("GET", "/plugins/arena/assets/<path:path>"),
    ("GET", "/api/v1/arena/events"),
    ("GET", "/api/v1/arena/events/by-slug/<slug>"),
    ("GET", "/api/v1/arena/events/<int:event_id>"),
    ("GET", "/api/v1/arena/events/<int:event_id>/announcements"),
    ("GET", "/api/v1/arena/events/<int:event_id>/scoreboard"),
    ("GET", "/api/v1/arena/events/<int:event_id>/scoreboard/categories"),
    ("GET", "/api/v1/arena/events/<int:event_id>/scoreboard/history"),
    ("GET", "/api/v1/arena/events/<int:event_id>/scoreboard/challenges"),
    ("GET", "/api/v1/arena/events/<int:event_id>/scoring"),  # public rules
    ("GET", "/api/v1/arena/certificates/verify/<public_id>"),  # needs the code
    ("GET", "/api/v1/arena/events/<int:event_id>/ad/scoreboard"),
    ("GET", "/api/v1/arena/events/<int:event_id>/ad/status"),
}
# additionally allowed for a logged-in outsider (not registered, no role)
OUTSIDER_OK = ANON_OK | {
    ("POST", "/api/v1/arena/events/<int:event_id>/register"),  # self-registration
    ("GET", "/api/v1/arena/me/events"),
    # Rules are swept in sorted order: my-score runs before register (returns
    # null for a non-participant); withdraw runs after it and withdraws the
    # outsider's OWN registration.
    ("POST", "/api/v1/arena/events/<int:event_id>/withdraw"),
    ("GET", "/api/v1/arena/events/<int:event_id>/my-score"),
    ("GET", "/api/v1/arena/events/<int:event_id>/teams"),  # after register
    ("GET", "/arena/admin"),  # renders, but shows nothing; API calls are guarded
}


class Cfg(TestingConfig):
    ARENA_CERTIFICATES_ENABLED = True


@pytest.fixture
def app():
    app = create_arena_ctfd(config=Cfg)
    app.config["RATELIMIT_DISABLE"] = True
    yield app
    destroy_ctfd(app)


def _url(rule, event_id, slug):
    def sub(m):
        conv, name = (m.group(1) or ""), m.group(2)
        if name == "event_id":
            return str(event_id)
        if name == "slug":
            return slug
        if name == "path":
            return "arena.js"
        if name == "section":
            return "scoreboard"
        if name == "kind":
            return "submissions"
        return "1" if "int" in conv else "x"

    return re.sub(r"<(?:(\w+(?:\([^)]*\))?):)?(\w+)>", sub, rule)


def _sweep(app, client, allowed):
    with app.app_context():
        from CTFd.plugins.arena.models import ArenaEvent

        ev = ArenaEvent.query.filter_by(slug="pub").one()
        eid, slug = ev.id, ev.slug
    bad = []
    n = 0
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):  # deterministic
        if not rule.rule.startswith(("/api/v1/arena", "/arena", "/plugins/arena")):
            continue
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            n += 1
            url = _url(rule.rule, eid, slug)
            kw = {"json": {}} if method in ("POST", "PATCH", "PUT", "DELETE") else {}
            r = client.open(url, method=method, **kw)
            if 200 <= r.status_code < 300 and (method, rule.rule) not in allowed:
                bad.append(f"{method} {rule.rule} -> {r.status_code}")
            assert r.status_code < 500, f"{method} {url} -> {r.status_code}"
    assert n > 100
    return bad


def _seed(app):
    with app.app_context():
        from CTFd.plugins.arena.services import challenges as chal_service

        ev = gen_event("pub", registration_open=True, ad_enabled=True)
        chal_service.create(
            ev,
            {
                "name": "c",
                "category": "x",
                "points": 100,
                "visible": True,
                "flags": [{"type": "static", "content": "flag{x}"}],
            },
        )
        from CTFd.models import db

        db.session.commit()
        make_user("outsider")


def test_anonymous_sweep(app):
    _seed(app)
    assert _sweep(app, app.test_client(), ANON_OK) == []


def test_outsider_sweep(app):
    _seed(app)
    c = login_as_user(app, name="outsider")
    assert _sweep(app, c, OUTSIDER_OK) == []

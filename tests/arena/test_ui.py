"""Phases 13-14: server-rendered dashboard shells and their access rules."""

import pathlib
import re

import pytest

from CTFd.config import TestingConfig
from tests.arena.helpers import (
    create_arena_ctfd,
    gen_event,
    gen_registration,
    gen_role,
    make_user,
)
from tests.helpers import destroy_ctfd, login_as_user

ASSETS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "CTFd"
    / "plugins"
    / "arena"
    / "assets"
)


@pytest.fixture
def app():
    app = create_arena_ctfd()
    app.config["RATELIMIT_LOGIN"] = (1000, 5)
    yield app
    destroy_ctfd(app)


def test_public_pages_and_assets(app):
    with app.app_context():
        gen_event("pub", description="**hello**")
    c = app.test_client()
    r = c.get("/arena")
    assert r.status_code == 200 and b'data-page="events"' in r.data
    assert b"/plugins/arena/assets/arena.js" in r.data
    r = c.get("/arena/e/pub")
    assert r.status_code == 200 and b'data-page="event-overview"' in r.data
    assert c.get("/arena/e/pub/scoreboard").status_code == 200
    assert c.get("/arena/e/pub/challenges").status_code == 302  # login required
    assert c.get("/plugins/arena/assets/arena.js").status_code == 200
    assert c.get("/plugins/arena/assets/arena.css").status_code == 200
    assert c.get("/arena/e/pub/nonsense").status_code == 404
    assert b"Competitions" in c.get("/").data  # navbar entry


def test_hidden_events_are_not_rendered(app):
    with app.app_context():
        gen_event("draft", status="draft")
        gen_event("priv", visibility="private")
        uid = make_user("member")
        gen_event("priv2", visibility="private")
    c = app.test_client()
    for slug in ("draft", "priv", "missing"):
        assert c.get(f"/arena/e/{slug}").status_code == 404
    with app.app_context():
        from CTFd.plugins.arena.models import ArenaEvent

        gen_registration(ArenaEvent.query.filter_by(slug="priv").one().id, uid)
    m = login_as_user(app, name="member")
    assert m.get("/arena/e/priv/challenges").status_code == 200
    assert m.get("/arena/e/priv2").status_code == 404


def test_mode_specific_tabs(app):
    with app.app_context():
        gen_event("jeop")
        gen_event(
            "adx", type="ad", ad_enabled=True, jeopardy_enabled=False, team_mode="team"
        )
        make_user("p")
    c = login_as_user(app, name="p")
    assert c.get("/arena/e/jeop/ad").status_code == 404
    assert c.get("/arena/e/adx/challenges").status_code == 404
    page = c.get("/arena/e/adx/ad").get_data(as_text=True)
    assert "Attack &amp; Defense" in page and "/arena/e/adx/challenges" not in page
    assert "/arena/e/jeop/team" not in c.get("/arena/e/jeop").get_data(
        as_text=True
    )  # individual event


def test_admin_shell_rbac(app):
    with app.app_context():
        a = gen_event("a").id
        b = gen_event("b").id
        gen_role(make_user("org"), "organizer", a)
        make_user("nobody")
    assert app.test_client().get("/arena/admin").status_code in (401, 403)
    assert login_as_user(app, name="nobody").get("/arena/admin").status_code == 403
    org = login_as_user(app, name="org")
    assert org.get("/arena/admin").status_code == 200
    page = org.get(f"/arena/admin/e/{a}").get_data(as_text=True)
    assert 'data-page="admin-event"' in page and "event.manage" in page
    assert "roles.manage" not in page  # superadmin-only permissions are never listed
    assert org.get(f"/arena/admin/e/{b}").status_code == 403
    assert org.get("/arena/admin/platform").status_code == 403
    admin = login_as_user(app, name="admin")
    assert admin.get("/arena/admin/platform").status_code == 200
    assert admin.get(f"/arena/admin/e/{b}").status_code == 200


def test_certificate_page_only_when_enabled(app):
    assert app.test_client().get("/arena/verify/abc").status_code == 404

    class Cfg(TestingConfig):
        ARENA_CERTIFICATES_ENABLED = True

    app2 = create_arena_ctfd(config=Cfg)
    try:
        r = app2.test_client().get("/arena/verify/abc?code=x")
        assert r.status_code == 200 and b'data-page="verify"' in r.data
    finally:
        destroy_ctfd(app2)


def test_page_data_attributes_are_escaped(app):
    with app.app_context():
        gen_event("xss", name='"><script>alert(1)</script>')
    page = app.test_client().get("/arena/e/xss").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page


def test_javascript_only_uses_innerhtml_for_sanitized_fields():
    """`html:` is only ever passed server-sanitized *_html fields."""
    for name in ("arena.js", "arena-admin.js"):
        src = (ASSETS / name).read_text()
        assert "innerHTML" not in src.replace(
            "el.innerHTML = v; // server-sanitized fields only", ""
        )
        for m in re.finditer(r"html:\s*([^,}\)]+)", src):
            value = m.group(1).strip()
            assert value.endswith(("_html", ".html")), f"{name}: html: {value}"
        assert "eval(" not in src and "document.write" not in src

"""Phase 2: event CRUD, lifecycle and visibility via the HTTP API."""

import datetime

import pytest

from CTFd.models import Challenges, ModuleAudienceAccess, Modules, db
from CTFd.plugins.arena.models import ArenaActivity, ArenaEvent
from CTFd.plugins.arena.services import events as event_service
from tests.arena.helpers import (
    create_arena_ctfd,
    gen_event,
    gen_registration,
    gen_role,
    make_user,
)
from tests.helpers import destroy_ctfd, gen_challenge, login_as_user

API = "/api/v1/arena"
NOW = datetime.datetime.utcnow()


def iso(dt):
    return dt.replace(microsecond=0).isoformat() + "Z"


@pytest.fixture
def app():
    app = create_arena_ctfd()
    app.config["RATELIMIT_LOGIN"] = (100, 5)
    yield app
    destroy_ctfd(app)


@pytest.fixture
def admin(app):
    return login_as_user(app, name="admin")


def payload(**kw):
    data = {
        "slug": "winter-ctf",
        "name": "Winter CTF",
        "description": "**Hello** <script>alert(1)</script>",
        "starts_at": iso(NOW + datetime.timedelta(days=1)),
        "ends_at": iso(NOW + datetime.timedelta(days=2)),
    }
    data.update(kw)
    return data


def test_create_event_creates_sealed_module_and_audits(app, admin):
    r = admin.post(f"{API}/admin/events", json=payload())
    assert r.status_code == 201, r.get_json()
    d = r.get_json()["data"]
    assert d["status"] == "draft" and d["slug"] == "winter-ctf"
    with app.app_context():
        e = db.session.get(ArenaEvent, d["id"])
        assert e.sealed_module_id is not None
        assert (
            ModuleAudienceAccess.query.filter_by(module_id=e.sealed_module_id).count()
            == 0
        )
        assert (
            ArenaActivity.query.filter_by(action="event.create", event_id=e.id).count()
            == 1
        )


def test_description_is_sanitized(app, admin):
    d = admin.post(f"{API}/admin/events", json=payload()).get_json()["data"]
    assert "<script>" not in d["description_html"]
    assert "<strong>Hello</strong>" in d["description_html"]


@pytest.mark.parametrize(
    "bad,field",
    [
        ({"slug": "Bad Slug!"}, "slug"),
        ({"slug": "a"}, "slug"),
        ({"name": ""}, "name"),
        ({"type": "koth"}, "type"),
        ({"ends_at": iso(NOW)}, "ends_at"),
        ({"starts_at": "tomorrow"}, "starts_at"),
        ({"max_participants": 0}, "max_participants"),
        ({"branding": {"logo_url": "javascript:alert(1)"}}, "branding.logo_url"),
        ({"branding": {"primary_color": "red"}}, "branding.primary_color"),
        ({"settings": {"nope": True}}, "settings"),
        (
            {"settings": {"scoreboard_refresh_seconds": 1}},
            "settings.scoreboard_refresh_seconds",
        ),
        ({"type": "ad", "team_mode": "individual"}, "team_mode"),
    ],
)
def test_create_validation(app, admin, bad, field):
    r = admin.post(f"{API}/admin/events", json=payload(**bad))
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["code"] == f"invalid:{field}"


def test_duplicate_slug_conflict(app, admin):
    assert admin.post(f"{API}/admin/events", json=payload()).status_code == 201
    r = admin.post(f"{API}/admin/events", json=payload())
    assert r.status_code == 409 and r.get_json()["code"] == "slug_taken"


def test_update_and_lifecycle(app, admin):
    eid = admin.post(f"{API}/admin/events", json=payload()).get_json()["data"]["id"]
    r = admin.patch(
        f"{API}/admin/events/{eid}", json={"name": "Renamed", "max_participants": 50}
    )
    assert r.status_code == 200 and r.get_json()["data"]["name"] == "Renamed"
    assert (
        admin.patch(f"{API}/admin/events/{eid}", json={"status": "active"}).status_code
        == 400
    )
    # draft -> active is not allowed directly
    assert (
        admin.post(
            f"{API}/admin/events/{eid}/status", json={"status": "active"}
        ).status_code
        == 409
    )
    for status in ("upcoming", "active", "paused", "active", "completed", "archived"):
        r = admin.post(f"{API}/admin/events/{eid}/status", json={"status": status})
        assert r.status_code == 200, (status, r.get_json())
    # type is locked after start
    assert (
        admin.patch(f"{API}/admin/events/{eid}", json={"type": "mixed"}).status_code
        == 409
    )


def test_publish_requires_times(app, admin):
    eid = admin.post(
        f"{API}/admin/events", json={"slug": "no-times", "name": "N"}
    ).get_json()["data"]["id"]
    r = admin.post(f"{API}/admin/events/{eid}/status", json={"status": "upcoming"})
    assert r.status_code == 409


def test_delete_only_when_safe(app, admin):
    eid = admin.post(f"{API}/admin/events", json=payload()).get_json()["data"]["id"]
    with app.app_context():
        module_id = db.session.get(ArenaEvent, eid).sealed_module_id
        uid = make_user("p")
        gen_registration(eid, uid)
    assert (
        admin.delete(f"{API}/admin/events/{eid}", json={}).status_code == 409
    )  # has registrations
    eid2 = admin.post(f"{API}/admin/events", json=payload(slug="other")).get_json()[
        "data"
    ]["id"]
    with app.app_context():
        m2 = db.session.get(ArenaEvent, eid2).sealed_module_id
        gen_challenge(db, module_id=m2)
    # Would unseal the challenge: refused.
    assert admin.delete(f"{API}/admin/events/{eid2}", json={}).status_code == 409
    with app.app_context():
        Challenges.query.filter_by(module_id=m2).delete()
        db.session.commit()
    assert admin.delete(f"{API}/admin/events/{eid2}", json={}).status_code == 200
    with app.app_context():
        assert db.session.get(ArenaEvent, eid2) is None
        assert db.session.get(Modules, m2) is None
        assert db.session.get(Modules, module_id) is not None
    admin.post(f"{API}/admin/events/{eid2}/status", json={"status": "upcoming"})


def test_non_draft_cannot_be_deleted(app, admin):
    eid = admin.post(f"{API}/admin/events", json=payload()).get_json()["data"]["id"]
    admin.post(f"{API}/admin/events/{eid}/status", json={"status": "upcoming"})
    assert admin.delete(f"{API}/admin/events/{eid}", json={}).status_code == 409


def test_effective_phase_follows_time_windows(app):
    with app.app_context():
        e = gen_event(
            "t",
            status="upcoming",
            starts_at=NOW - datetime.timedelta(hours=1),
            ends_at=NOW + datetime.timedelta(hours=1),
        )
        assert event_service.effective_phase(e) == "active"
        assert (
            event_service.effective_phase(e, NOW + datetime.timedelta(hours=2))
            == "completed"
        )
        assert (
            event_service.effective_phase(e, NOW - datetime.timedelta(hours=2))
            == "upcoming"
        )
        e.status = "paused"
        assert event_service.effective_phase(e) == "paused"  # pause is never overridden
        e.status = "upcoming"
        db.session.commit()
        changed = event_service.sync_statuses()
        assert changed == [(e.id, "upcoming", "active")]
        assert db.session.get(ArenaEvent, e.id).status == "active"


def test_public_listing_hides_drafts_private_and_unlisted(app):
    with app.app_context():
        gen_event("pub")
        gen_event("draft", status="draft")
        gen_event("priv", visibility="private")
        gen_event("unl", visibility="unlisted")
    r = app.test_client().get(f"{API}/events")
    assert [e["slug"] for e in r.get_json()["data"]] == ["pub"]
    assert r.get_json()["meta"]["pagination"]["total"] == 1


def test_private_event_listed_for_registrant_and_roles(app):
    with app.app_context():
        priv = gen_event("priv", visibility="private")
        gen_event("pub")
        uid = make_user("member")
        gen_registration(priv.id, uid)
        oid = make_user("organizer")
        draft = gen_event("draft", status="draft")
        gen_role(oid, "organizer", draft.id)
    c = login_as_user(app, name="member")
    assert sorted(e["slug"] for e in c.get(f"{API}/events").get_json()["data"]) == [
        "priv",
        "pub",
    ]
    c = login_as_user(app, name="organizer")
    assert sorted(e["slug"] for e in c.get(f"{API}/events").get_json()["data"]) == [
        "draft",
        "pub",
    ]


def test_slug_lookup_respects_visibility(app):
    with app.app_context():
        gen_event("priv", visibility="private")
        gen_event("unl", visibility="unlisted")
    c = app.test_client()
    assert c.get(f"{API}/events/by-slug/priv").status_code == 404
    assert c.get(f"{API}/events/by-slug/unl").status_code == 200
    assert c.get(f"{API}/events/by-slug/missing").status_code == 404


def test_rbac_on_admin_endpoints(app):
    with app.app_context():
        a = gen_event("a").id
        b = gen_event("b").id
        org = make_user("org")
        gen_role(org, "organizer", a)
        ana = make_user("ana")
        gen_role(ana, "analyst", a)
        make_user("nobody")
    org_c = login_as_user(app, name="org")
    assert (
        org_c.patch(f"{API}/admin/events/{a}", json={"name": "ok"}).status_code == 200
    )
    assert org_c.patch(f"{API}/admin/events/{b}", json={"name": "x"}).status_code == 403
    assert (
        org_c.post(f"{API}/admin/events", json=payload()).status_code == 403
    )  # superadmin only
    assert (
        org_c.post(
            f"{API}/admin/roles",
            json={"user_id": org, "role": "organizer", "event_id": b},
        ).status_code
        == 403
    )
    assert [e["id"] for e in org_c.get(f"{API}/admin/events").get_json()["data"]] == [a]
    ana_c = login_as_user(app, name="ana")
    assert ana_c.get(f"{API}/admin/events/{a}").status_code == 200
    assert ana_c.patch(f"{API}/admin/events/{a}", json={"name": "x"}).status_code == 403
    assert ana_c.get(f"{API}/admin/events/{a}/registrations").status_code == 200
    assert ana_c.post(f"{API}/admin/events/{a}/invites", json={}).status_code == 403
    nob = login_as_user(app, name="nobody")
    assert nob.get(f"{API}/admin/events").status_code == 403
    assert nob.get(f"{API}/admin/events/{a}").status_code == 403
    assert app.test_client().get(f"{API}/admin/events").status_code == 401


def test_roles_api_superadmin(app, admin):
    with app.app_context():
        a = gen_event("a").id
        uid = make_user("x")
    r = admin.post(
        f"{API}/admin/roles",
        json={"user_id": uid, "role": "challenge_manager", "event_id": a},
    )
    assert r.status_code == 201
    rid = r.get_json()["data"]["id"]
    assert (
        admin.post(
            f"{API}/admin/roles", json={"user_id": uid, "role": "god"}
        ).status_code
        == 400
    )
    assert (
        admin.post(
            f"{API}/admin/roles", json={"user_id": 99999, "role": "analyst"}
        ).status_code
        == 404
    )
    assert len(admin.get(f"{API}/admin/roles").get_json()["data"]) == 1
    assert admin.delete(f"{API}/admin/roles/{rid}", json={}).status_code == 200
    with app.app_context():
        assert (
            ArenaActivity.query.filter(ArenaActivity.action.like("role.%")).count() == 2
        )


def test_api_errors_are_json_and_not_leaky(app, admin):
    r = admin.get(f"{API}/admin/events/999999")
    assert r.status_code == 404 and r.is_json and r.get_json()["success"] is False
    r = admin.post(
        f"{API}/admin/events", data="not json", content_type="application/json"
    )
    assert r.status_code in (400, 403)
    assert b"Traceback" not in r.data

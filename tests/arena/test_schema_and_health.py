import pytest
from flask import jsonify

from CTFd.plugins.arena import dbmigrate
from CTFd.plugins.arena.core import schema as schema_guard
from tests.arena.helpers import create_arena_ctfd
from tests.helpers import destroy_ctfd

guarded = schema_guard.arena_blueprint("arena_test_guarded", __name__)


@guarded.route("/t/guarded")
def t_guarded():
    return jsonify({"ok": True})


@pytest.fixture
def app():
    app = create_arena_ctfd(blueprints=[guarded])
    yield app
    destroy_ctfd(app)


def test_healthz_no_io(app):
    r = app.test_client().get("/arena/healthz")
    assert r.status_code == 200 and r.get_json()["status"] == "ok"


def test_readyz_ok(app):
    r = app.test_client().get("/arena/readyz")
    assert r.status_code == 200
    assert r.get_json() == {
        "ready": True,
        "checks": {"db": True, "cache": True, "schema": True},
    }
    assert r.headers["Cache-Control"] == "no-store"


def test_readyz_reports_cache_failure_without_500(app, monkeypatch):
    from CTFd.plugins.arena import health

    real_set = health.cache.set

    def flaky_set(key, *a, **k):
        # Fail only Arena's probe key: native sessions also live in the cache.
        if str(key).startswith("arena:readyz:"):
            raise ConnectionError("redis down")
        return real_set(key, *a, **k)

    monkeypatch.setattr(health.cache, "set", flaky_set)
    r = app.test_client().get("/arena/readyz")
    assert r.status_code == 503
    assert r.get_json()["checks"]["cache"] is False


def test_readyz_reports_db_failure_without_500(app, monkeypatch):
    from CTFd.plugins.arena import health

    monkeypatch.setattr(health, "check_db", lambda: False)
    r = app.test_client().get("/arena/readyz")
    assert r.status_code == 503
    assert r.get_json()["checks"] == {"db": False, "cache": True, "schema": False}


def test_schema_guard_returns_503_when_schema_missing(app, monkeypatch):
    c = app.test_client()
    assert c.get("/t/guarded").status_code == 200
    schema_guard.reset_cache()
    monkeypatch.setattr(dbmigrate, "schema_state", lambda: (False, {}))
    r = c.get("/t/guarded")
    assert r.status_code == 503
    assert "flask arena migrate" in r.get_json()["errors"][0]


def test_schema_guard_caches_positive_result(app, monkeypatch):
    c = app.test_client()
    schema_guard.reset_cache()
    calls = []
    real = dbmigrate.schema_state

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(dbmigrate, "schema_state", counting)
    for _ in range(5):
        assert c.get("/t/guarded").status_code == 200
    assert len(calls) == 1


def test_schema_guard_does_not_cache_negative_result(app, monkeypatch):
    c = app.test_client()
    schema_guard.reset_cache()
    state = {"ok": False}
    monkeypatch.setattr(dbmigrate, "schema_state", lambda: (state["ok"], {}))
    assert c.get("/t/guarded").status_code == 503
    state["ok"] = True  # operator ran the migration
    assert c.get("/t/guarded").status_code == 200

import json

import pytest

from CTFd.models import Audiences, ModuleAudienceAccess, Modules, db
from CTFd.plugins.arena import dbmigrate
from tests.arena.helpers import admin_user, create_arena_ctfd, gen_event, gen_role
from tests.helpers import destroy_ctfd


@pytest.fixture
def app():
    app = create_arena_ctfd()
    yield app
    destroy_ctfd(app)


def run(app, *args):
    return app.test_cli_runner().invoke(args=["arena", *args])


def test_cli_registered(app):
    assert "arena" in app.cli.commands


def test_db_current(app):
    r = run(app, "db-current")
    assert r.exit_code == 0
    data = json.loads(r.output.strip().splitlines()[-1])
    assert data["up_to_date"] is True
    assert data["head"] == dbmigrate.head_revision()


def test_downgrade_requires_confirmation(app):
    r = run(app, "db-downgrade", "base")
    assert r.exit_code == 2
    with app.app_context():
        assert len(dbmigrate.existing_arena_tables()) == len(dbmigrate.arena_tables())


def test_downgrade_base_then_migrate(app):
    r = run(app, "db-downgrade", "base", "--yes")
    assert r.exit_code == 0, r.output
    with app.app_context():
        assert dbmigrate.existing_arena_tables() == []
    assert app.test_client().get("/arena/readyz").status_code == 503
    r = run(app, "migrate")
    assert r.exit_code == 0, r.output
    with app.app_context():
        assert len(dbmigrate.existing_arena_tables()) == len(dbmigrate.arena_tables())


def test_downgrade_unknown_revision_errors(app):
    r = run(app, "db-downgrade", "deadbeef0000", "--yes")
    assert r.exit_code != 0
    with app.app_context():
        assert len(dbmigrate.existing_arena_tables()) == len(dbmigrate.arena_tables())


def test_check_integrity_passes_on_clean_db(app):
    with app.app_context():
        gen_event("ok")
    r = run(app, "check-integrity")
    assert r.exit_code == 0, r.output
    assert "[FAIL]" not in r.output


def test_check_integrity_detects_unsealed_module(app):
    with app.app_context():
        m = Modules(name="sealed")
        aud = Audiences(name="leak")
        db.session.add_all([m, aud])
        db.session.commit()
        gen_event("a", sealed_module_id=m.id)
        db.session.add(ModuleAudienceAccess(module_id=m.id, audience_id=aud.id))
        db.session.commit()
    r = run(app, "check-integrity")
    assert r.exit_code == 1
    assert "[FAIL] sealed_modules" in r.output
    assert "exposed to native CTFd" in r.output


def test_check_integrity_detects_bad_windows_and_redundant_roles(app):
    import datetime

    with app.app_context():
        t = datetime.datetime.utcnow()
        gen_event("bad", starts_at=t, ends_at=t - datetime.timedelta(hours=1))
        gen_role(admin_user().id, "analyst")
    r = run(app, "check-integrity")
    assert r.exit_code == 1
    assert "starts_at >= ends_at" in r.output
    assert "redundant" in r.output


def test_worker_health_exit_codes(app):
    import socket

    from CTFd.plugins.arena.worker import Worker

    r = run(app, "worker-health")
    assert r.exit_code == 1 and json.loads(r.output)["alive"] == 0
    with app.app_context():
        w = Worker(app, schedule=False)
        w.run(max_iterations=1)  # registers + heartbeats, then stops
        db.session.remove()
    # a stopped worker no longer counts as alive
    r = run(app, "worker-health")
    assert r.exit_code == 1
    with app.app_context():
        from CTFd.plugins.arena.models import ArenaWorker

        row = db.session.get(ArenaWorker, w.id)
        row.status = "running"
        db.session.commit()
    r = run(app, "worker-health", "--host")
    assert r.exit_code == 0 and json.loads(r.output)["alive"] == 1
    assert json.loads(r.output)["workers"][0]["id"].startswith(
        socket.gethostname()[:40]
    )

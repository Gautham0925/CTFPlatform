"""Arena must not change native CTFd behaviour."""

from CTFd.config import TestingConfig
from CTFd.models import Challenges
from tests.arena.helpers import create_arena_ctfd
from tests.helpers import (
    create_ctfd,
    destroy_ctfd,
    gen_challenge,
    gen_flag,
    login_as_user,
    register_user,
)


def test_native_challenge_flow_unchanged_with_arena_loaded():
    app = create_arena_ctfd()
    try:
        with app.app_context():
            gen_challenge(app.db)
            gen_flag(app.db, challenge_id=1, content="flag")
            register_user(app)
            c = login_as_user(app)
            assert c.get("/api/v1/challenges").status_code == 200
            r = c.post(
                "/api/v1/challenges/attempt",
                json={"challenge_id": 1, "submission": "flag"},
            )
            assert r.get_json()["data"]["status"] == "correct"
            assert c.get("/api/v1/scoreboard").status_code == 200
            assert c.get("/healthcheck").status_code == 200
    finally:
        destroy_ctfd(app)


def test_safe_mode_does_not_load_arena():
    app = create_ctfd(enable_plugins=False)
    try:
        assert "arena" not in app.extensions
        assert app.test_client().get("/arena/healthz").status_code == 404
    finally:
        destroy_ctfd(app)


def test_arena_disabled_registers_cli_only():
    class Cfg(TestingConfig):
        ARENA_ENABLED = False

    app = create_ctfd(enable_plugins=True, config=Cfg)
    try:
        assert app.extensions["arena"]["settings"].enabled is False
        assert "arena" in app.cli.commands
        assert app.test_client().get("/arena/healthz").status_code == 404
    finally:
        destroy_ctfd(app)


def test_no_native_table_gains_columns():
    """Arena adds tables only; native models are unchanged."""
    native_cols = {c.name for c in Challenges.__table__.columns}
    assert not any(c.startswith("arena") for c in native_cols)

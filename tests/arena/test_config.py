import pytest
from flask import Flask

from CTFd.plugins.arena.config import ArenaConfigError, ArenaSettings


def _app(**cfg):
    app = Flask(__name__)
    app.config.update(cfg)
    return app


def test_defaults(monkeypatch):
    for k in (
        "ARENA_ENABLED",
        "ARENA_CERTIFICATES_ENABLED",
        "ARENA_RUNNER_MODE",
        "ARENA_SCHEMA_CHECK_TTL",
    ):
        monkeypatch.delenv(k, raising=False)
    s = ArenaSettings.from_app(_app())
    assert s.enabled is True
    assert s.certificates_enabled is False  # optional, off by default
    assert s.runner_mode == "mock"  # real runner arrives in Phase 8
    assert s.schema_check_ttl == 30


def test_env_and_app_config(monkeypatch):
    monkeypatch.setenv("ARENA_CERTIFICATES_ENABLED", "true")
    assert ArenaSettings.from_app(_app()).certificates_enabled is True
    # app.config wins over env
    assert (
        ArenaSettings.from_app(
            _app(ARENA_CERTIFICATES_ENABLED=False)
        ).certificates_enabled
        is False
    )


@pytest.mark.parametrize(
    "cfg",
    [
        {"ARENA_RUNNER_MODE": "kubernetes"},  # unknown mode
        {"ARENA_RUNNER_MODE": "docker"},
        {"ARENA_ENABLED": "maybe"},
        {"ARENA_SCHEMA_CHECK_TTL": "-1"},
        {"ARENA_SCHEMA_CHECK_TTL": "soon"},
    ],
)
def test_invalid_values_fail_loudly(cfg):
    with pytest.raises(ArenaConfigError):
        ArenaSettings.from_app(_app(**cfg))


def test_arena_env_vars_reach_app_config(monkeypatch):
    """Regression (Phase 16): ARENA_* set only in the environment (as every
    compose profile does) were ignored because CTFd does not load unknown keys."""
    from CTFd.plugins.arena.config import ArenaConfigError, import_environment

    class App:
        config = {"ARENA_PUBLIC_URL": "https://explicit.example"}

    env = {
        "ARENA_SECRET_KEY": "k" * 40,
        "ARENA_PUBLIC_URL": "https://from-env.example",
        "ARENA_LAB_ALLOWED_PRIVATE_HOSTS": "",
        "ARENA_ACTIVITY_RETENTION_DAYS": "30",
        "UNRELATED": "x",
    }
    imported = import_environment(App, env)
    assert imported == ["ARENA_ACTIVITY_RETENTION_DAYS", "ARENA_SECRET_KEY"]
    assert App.config["ARENA_SECRET_KEY"] == "k" * 40
    assert App.config["ARENA_PUBLIC_URL"] == "https://explicit.example"  # config wins
    assert "ARENA_LAB_ALLOWED_PRIVATE_HOSTS" not in App.config  # empty = unset
    assert "UNRELATED" not in App.config

    class Weak:
        config = {}

    with pytest.raises(ArenaConfigError):
        import_environment(Weak, {"ARENA_SECRET_KEY": "short"})


def test_lab_credentials_use_arena_secret_key_from_env(monkeypatch):
    from tests.arena.helpers import create_arena_ctfd
    from tests.helpers import destroy_ctfd

    monkeypatch.setenv("ARENA_SECRET_KEY", "e" * 48)
    app = create_arena_ctfd()
    try:
        assert app.config["ARENA_SECRET_KEY"] == "e" * 48
        with app.app_context():
            from CTFd.plugins.arena.labs import crypto

            token = crypto.encrypt({"token": "t"})
            assert crypto.decrypt(token) == {"token": "t"}
            app.config["ARENA_SECRET_KEY"] = "f" * 48  # proves the env key was used
            assert crypto.decrypt(token) is None  # wrong key -> not decryptable
    finally:
        destroy_ctfd(app)

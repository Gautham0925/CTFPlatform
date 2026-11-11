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
        {"ARENA_RUNNER_MODE": "remote"},  # not available until Phase 8
        {"ARENA_RUNNER_MODE": "docker"},
        {"ARENA_ENABLED": "maybe"},
        {"ARENA_SCHEMA_CHECK_TTL": "-1"},
        {"ARENA_SCHEMA_CHECK_TTL": "soon"},
    ],
)
def test_invalid_values_fail_loudly(cfg):
    with pytest.raises(ArenaConfigError):
        ArenaSettings.from_app(_app(**cfg))

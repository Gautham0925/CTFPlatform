"""Arena settings.

Values are read from the Flask app config first, then from environment
variables of the same name. Everything has a safe default so that Arena can be
loaded on an unconfigured CTFd without changing native behaviour.
"""

import os
from dataclasses import dataclass

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}

RUNNER_MODES = ("mock",)  # "remote" is introduced in Phase 8


class ArenaConfigError(ValueError):
    pass


def _raw(app, key, default):
    if key in app.config:
        return app.config[key]
    return os.environ.get(key, default)


def _bool(app, key, default):
    value = _raw(app, key, default)
    if isinstance(value, bool):
        return value
    value = str(value).strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ArenaConfigError(f"{key} must be a boolean, got {value!r}")


def _int(app, key, default, minimum=None):
    value = _raw(app, key, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ArenaConfigError(f"{key} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ArenaConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class ArenaSettings:
    enabled: bool
    certificates_enabled: bool
    runner_mode: str
    schema_check_ttl: int

    @classmethod
    def from_app(cls, app):
        runner_mode = str(_raw(app, "ARENA_RUNNER_MODE", "mock")).strip().lower()
        if runner_mode not in RUNNER_MODES:
            raise ArenaConfigError(
                f"ARENA_RUNNER_MODE={runner_mode!r} is not supported; "
                f"allowed: {', '.join(RUNNER_MODES)}"
            )
        return cls(
            enabled=_bool(app, "ARENA_ENABLED", True),
            # Certificates are optional and disabled by default.
            certificates_enabled=_bool(app, "ARENA_CERTIFICATES_ENABLED", False),
            runner_mode=runner_mode,
            schema_check_ttl=_int(app, "ARENA_SCHEMA_CHECK_TTL", 30, minimum=0),
        )


def get_settings(app):
    return app.extensions["arena"]["settings"]

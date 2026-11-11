"""Arena settings.

Values are read from the Flask app config first, then from environment
variables of the same name. Everything has a safe default so that Arena can be
loaded on an unconfigured CTFd without changing native behaviour.
"""

import os
from dataclasses import dataclass

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}

RUNNER_MODES = ("mock", "remote")  # remote = runner/ service over HTTPS+HMAC


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


def import_environment(app, environ=None):
    """Copy ARENA_* environment variables into ``app.config``.

    CTFd only loads the keys it knows about into its config, so without this
    an ``ARENA_SECRET_KEY`` (or retention, lab, flag-secret ...) set in the
    container environment would be silently ignored. Explicit app config
    wins; empty values are treated as unset.
    """
    environ = os.environ if environ is None else environ
    imported = []
    for key, value in environ.items():
        if key.startswith("ARENA_") and key not in app.config and value != "":
            app.config[key] = value
            imported.append(key)
    secret = app.config.get("ARENA_SECRET_KEY")
    if secret is not None and len(str(secret)) < 32:
        raise ArenaConfigError("ARENA_SECRET_KEY must be at least 32 characters")
    return sorted(imported)


def get_settings(app):
    return app.extensions["arena"]["settings"]

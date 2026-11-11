"""Per-event settings: a closed, validated schema with defaults.

Unknown keys are rejected so typos never silently disable a control.
"""

from CTFd.plugins.arena.validation import EMAIL_DOMAIN_RE, ValidationError

# key -> (kind, default, extra)
SCHEMA = {
    # Registration / eligibility (Phase 2)
    "require_verified_email": ("bool", False, None),
    "allowed_email_domains": ("domains", [], None),
    "invite_only": ("bool", False, None),
    # Teams (Phase 3)
    "lock_teams_after_start": ("bool", False, None),
    "default_team_visibility": ("choice", "public", ("public", "private")),
    # Jeopardy (Phase 4)
    "max_attempts_default": ("int", 0, (0, 1000)),
    "submissions_per_minute": ("int", 10, (1, 120)),
    "per_entity_flags": ("bool", False, None),
    # Scoring / scoreboard (Phase 5)
    "individual_scoring": ("bool", False, None),
    "first_blood_bonus": ("int", 0, (0, 100000)),
    "scoreboard_visibility": (
        "choice",
        "public",
        ("public", "participants", "admins"),
    ),
    "scoreboard_refresh_seconds": ("int", 15, (5, 300)),
    "show_solve_counts": ("bool", True, None),
    # Attack & Defense (Phase 9)
    "ad_round_seconds": ("int", 120, (30, 3600)),
    "ad_flag_lifetime_rounds": ("int", 5, (1, 50)),
    "ad_warmup_rounds": ("int", 0, (0, 20)),
    "ad_flag_submissions_per_minute": ("int", 60, (1, 1000)),
    # Labs / instances (Phases 8, 11)
    "max_instances_per_entity": ("int", 2, (0, 20)),
    "instance_ttl_minutes": ("int", 60, (5, 1440)),
}


def defaults():
    return {
        k: (list(v[1]) if isinstance(v[1], list) else v[1]) for k, v in SCHEMA.items()
    }


def effective(event) -> dict:
    merged = defaults()
    merged.update(event.settings or {})
    return merged


def get(event, key):
    if key not in SCHEMA:
        raise KeyError(key)
    return effective(event)[key]


def _coerce(key, value):
    kind, _default, extra = SCHEMA[key]
    field = f"settings.{key}"
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValidationError(field, "must be a boolean")
        return value
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(field, "must be an integer")
        lo, hi = extra
        if not lo <= value <= hi:
            raise ValidationError(field, f"must be between {lo} and {hi}")
        return value
    if kind == "choice":
        if value not in extra:
            raise ValidationError(field, f"must be one of: {', '.join(extra)}")
        return value
    if kind == "domains":
        if not isinstance(value, list) or len(value) > 50:
            raise ValidationError(field, "must be a list of at most 50 domains")
        out = []
        for d in value:
            if not isinstance(d, str) or not EMAIL_DOMAIN_RE.match(d.strip().lower()):
                raise ValidationError(field, f"invalid domain: {d!r}")
            out.append(d.strip().lower())
        return out
    raise AssertionError(kind)  # pragma: no cover


def validate_update(current: dict, changes) -> dict:
    """Return the new stored settings (only non-default overrides are kept)."""
    if not isinstance(changes, dict):
        raise ValidationError("settings", "must be an object")
    unknown = sorted(set(changes) - set(SCHEMA))
    if unknown:
        raise ValidationError("settings", f"unknown keys: {', '.join(unknown)}")
    stored = dict(current or {})
    for key, value in changes.items():
        stored[key] = _coerce(key, value)
    return stored

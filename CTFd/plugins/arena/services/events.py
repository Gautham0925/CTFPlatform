"""Event lifecycle service.

Stored status is the organizer-controlled lifecycle. ``effective_phase`` also
applies the configured time window, so participation rules are correct even
if the background worker that syncs stored statuses is not running.
"""

import datetime

from sqlalchemy.exc import IntegrityError

from CTFd.models import Challenges, ModuleAudienceAccess, Modules, db
from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.api import ApiError
from CTFd.plugins.arena.models import (
    EVENT_TYPES,
    EVENT_VISIBILITIES,
    TEAM_MODES,
    ArenaEvent,
    ArenaRegistration,
)
from CTFd.plugins.arena.services import settings as event_settings

# Allowed organizer-driven transitions.
TRANSITIONS = {
    "draft": {"upcoming", "archived"},
    "upcoming": {"draft", "active", "archived"},
    "active": {"paused", "completed"},
    "paused": {"active", "completed"},
    "completed": {"archived", "active"},
    "archived": {"completed"},
}

# Callables run before an event is deleted (later phases register cleanup).
DELETE_HOOKS = []


def utcnow():
    return datetime.datetime.utcnow()


def effective_phase(event, now=None):
    now = now or utcnow()
    status = event.status
    if status in ("upcoming", "active"):
        if event.ends_at and now >= event.ends_at:
            return "completed"
        if status == "upcoming" and event.starts_at and now >= event.starts_at:
            return "active"
        if status == "active" and event.starts_at and now < event.starts_at:
            return "upcoming"
    return status


def is_running(event, now=None):
    """Participants may compete (submit, allocate labs, ...)."""
    return effective_phase(event, now) == "active"


# ----------------------------------------------------------------- validation

EDITABLE_TEXT = {"name": 128, "description": 20000, "rules": 20000}


def _validate_branding(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise v.ValidationError("branding", "must be an object")
    allowed = {"primary_color", "accent_color", "tagline", "logo_url"}
    unknown = set(value) - allowed
    if unknown:
        raise v.ValidationError(
            "branding", f"unknown keys: {', '.join(sorted(unknown))}"
        )
    out = {}
    for key in ("primary_color", "accent_color"):
        if value.get(key):
            if not v.HEX_COLOR_RE.match(str(value[key])):
                raise v.ValidationError(f"branding.{key}", "must be #rrggbb")
            out[key] = value[key]
    if value.get("tagline"):
        out["tagline"] = v.string(value, "tagline", max_len=200)
    if value.get("logo_url"):
        url = v.string(value, "logo_url", max_len=500)
        # Only same-site paths or https URLs (no javascript:, data:, http:).
        if not (
            url.startswith("/") and not url.startswith("//")
        ) and not url.startswith("https://"):
            raise v.ValidationError("branding.logo_url", "must be https:// or a /path")
        out["logo_url"] = url
    return out


def _apply_fields(event, data, creating):
    if creating or "slug" in data:
        slug = v.slug(data, required=creating)
        if slug is not None:
            event.slug = slug
    for field, max_len in EDITABLE_TEXT.items():
        if field in data:
            event_value = v.string(
                data,
                field,
                max_len=max_len,
                required=(creating and field == "name"),
                allow_empty=(field != "name"),
            )
            setattr(event, field, event_value)
    if creating and not data.get("name"):
        raise v.ValidationError("name", "is required")
    if "type" in data:
        event.type = v.choice(data, "type", EVENT_TYPES)
    if "visibility" in data:
        event.visibility = v.choice(data, "visibility", EVENT_VISIBILITIES)
    if "team_mode" in data:
        event.team_mode = v.choice(data, "team_mode", TEAM_MODES)
    for field in (
        "starts_at",
        "ends_at",
        "freeze_at",
        "registration_opens_at",
        "registration_closes_at",
    ):
        if field in data:
            setattr(event, field, v.dt(data, field))
    for field in (
        "registration_open",
        "requires_approval",
        "jeopardy_enabled",
        "ad_enabled",
    ):
        if field in data:
            setattr(event, field, v.boolean(data, field))
    if "max_participants" in data:
        event.max_participants = v.integer(
            data, "max_participants", minimum=1, maximum=100000
        )
    for field in ("team_size_min", "team_size_max"):
        if field in data:
            setattr(
                event,
                field,
                v.integer(data, field, minimum=1, maximum=100, required=True),
            )
    if "branding" in data:
        event.branding = _validate_branding(data["branding"])
    if "settings" in data:
        event.settings = event_settings.validate_update(
            event.settings, data["settings"]
        )


def _check_invariants(event):
    if event.starts_at and event.ends_at and event.starts_at >= event.ends_at:
        raise v.ValidationError("ends_at", "must be after starts_at")
    if event.freeze_at and (
        (event.starts_at and event.freeze_at < event.starts_at)
        or (event.ends_at and event.freeze_at > event.ends_at)
    ):
        raise v.ValidationError("freeze_at", "must be within the event window")
    if (
        event.registration_opens_at
        and event.registration_closes_at
        and event.registration_opens_at >= event.registration_closes_at
    ):
        raise v.ValidationError(
            "registration_closes_at", "must be after registration_opens_at"
        )
    if event.team_size_max < event.team_size_min:
        raise v.ValidationError("team_size_max", "must be >= team_size_min")
    if event.team_mode == "individual" and (
        event.team_size_min,
        event.team_size_max,
    ) != (1, 1):
        event.team_size_min = event.team_size_max = 1
    if event.type == "jeopardy":
        event.jeopardy_enabled, event.ad_enabled = True, False
    elif event.type == "ad":
        event.jeopardy_enabled, event.ad_enabled = False, True
    if event.ad_enabled and event.team_mode != "team":
        raise v.ValidationError("team_mode", "Attack & Defense requires team mode")
    if (
        event.max_participants is not None
        and event.seat_count
        and event.max_participants < event.seat_count
    ):
        raise v.ValidationError(
            "max_participants", f"cannot be below current seats ({event.seat_count})"
        )


# ---------------------------------------------------------------- operations


def ensure_sealed_module(event):
    """Guarantee the event has its sealed module (fail closed).

    Events are normally created with one, but events created by other paths
    (imports, direct DB inserts) would otherwise make challenges unsealed.
    """
    if event.sealed_module_id is not None:
        return event.sealed_module_id
    module = Modules(
        name=f"arena:{event.slug}"[:80],
        description="Sealed Arena event module. Never add audiences: doing so "
        "exposes event challenges to native CTFd.",
    )
    db.session.add(module)
    db.session.flush()
    event.sealed_module_id = module.id
    db.session.flush()
    return module.id


def create_event(data, actor):
    event = ArenaEvent(
        status="draft",
        type="jeopardy",
        visibility="public",
        team_mode="individual",
        team_size_min=1,
        team_size_max=1,
        registration_open=False,
        requires_approval=False,
        jeopardy_enabled=True,
        ad_enabled=False,
        seat_count=0,
        created_by=actor.id if actor else None,
    )
    _apply_fields(event, data, creating=True)
    _check_invariants(event)
    module = Modules(
        name=f"arena:{event.slug}"[:80],
        description="Sealed Arena event module. Never add audiences: doing so "
        "exposes event challenges to native CTFd.",
    )
    db.session.add(module)
    db.session.flush()
    event.sealed_module_id = module.id
    db.session.add(event)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError(409, "An event with this slug already exists", code="slug_taken")
    return event


LOCKED_WHEN_RUNNING = {"type", "team_mode", "slug"}


def update_event(event, data):
    if effective_phase(event) in ("active", "paused", "completed", "archived"):
        locked = sorted(LOCKED_WHEN_RUNNING & set(data))
        if locked:
            raise ApiError(
                409, f"Cannot change {', '.join(locked)} after the event started"
            )
    if "team_mode" in data and event.seat_count:
        has_regs = ArenaRegistration.query.filter_by(event_id=event.id).first()
        if has_regs and data["team_mode"] != event.team_mode:
            raise ApiError(409, "Cannot change team_mode once participants registered")
    _apply_fields(event, data, creating=False)
    _check_invariants(event)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError(409, "An event with this slug already exists", code="slug_taken")
    return event


def transition(event, to_status):
    if to_status not in TRANSITIONS:
        raise v.ValidationError("status", "unknown status")
    if to_status == event.status:
        return event
    if to_status not in TRANSITIONS[event.status]:
        raise ApiError(409, f"Cannot move event from {event.status} to {to_status}")
    if to_status in ("upcoming", "active") and not (event.starts_at and event.ends_at):
        raise ApiError(409, "starts_at and ends_at must be set before publishing")
    if (
        to_status == "draft"
        and ArenaRegistration.query.filter_by(event_id=event.id).first()
    ):
        raise ApiError(409, "Cannot unpublish an event that has registrations")
    event.status = to_status
    db.session.commit()
    return event


def delete_event(event):
    """Hard delete only when safe: a draft with no registrations."""
    if event.status != "draft":
        raise ApiError(409, "Only draft events can be deleted; archive it instead")
    if ArenaRegistration.query.filter_by(event_id=event.id).first():
        raise ApiError(409, "Event has registrations; archive it instead")
    for hook in DELETE_HOOKS:
        hook(event)
    module_id = event.sealed_module_id
    if module_id and Challenges.query.filter_by(module_id=module_id).first():
        # Deleting the module would SET NULL on challenges and unseal them.
        raise ApiError(409, "Event still owns challenges; remove them first")
    db.session.delete(event)
    db.session.flush()
    if module_id:
        ModuleAudienceAccess.query.filter_by(module_id=module_id).delete()
        Modules.query.filter_by(id=module_id).delete()
    db.session.commit()


def sync_statuses(now=None):
    """Persist time-driven transitions (run by the worker). Returns changes."""
    now = now or utcnow()
    changed = []
    for event in ArenaEvent.query.filter(ArenaEvent.status.in_(("upcoming", "active"))):
        phase = effective_phase(event, now)
        if phase != event.status and phase in ("active", "completed"):
            changed.append((event.id, event.status, phase))
            event.status = phase
    if changed:
        db.session.commit()
    return changed


# ------------------------------------------------------------- serialization


def render_markdown(text):
    if not text:
        return ""
    from CTFd.utils.config.pages import build_markdown

    # Always sanitized: event organizers are not trusted with raw HTML.
    return build_markdown(text, sanitize=True)


def serialize_public(event, registration=None, now=None):
    seats_left = None
    if event.max_participants is not None:
        seats_left = max(0, event.max_participants - (event.seat_count or 0))
    s = event_settings.effective(event)
    return {
        "id": event.id,
        "slug": event.slug,
        "name": event.name,
        "description_html": render_markdown(event.description),
        "rules_html": render_markdown(event.rules),
        "type": event.type,
        "status": event.status,
        "phase": effective_phase(event, now),
        "visibility": event.visibility,
        "team_mode": event.team_mode,
        "team_size_min": event.team_size_min,
        "team_size_max": event.team_size_max,
        "starts_at": v.iso(event.starts_at),
        "ends_at": v.iso(event.ends_at),
        "freeze_at": v.iso(event.freeze_at),
        "registration": {
            "open": registration_window_open(event, now),
            "opens_at": v.iso(event.registration_opens_at),
            "closes_at": v.iso(event.registration_closes_at or event.ends_at),
            "requires_approval": event.requires_approval,
            "invite_required": event.visibility == "private" or s["invite_only"],
            "seats_left": seats_left,
            "max_participants": event.max_participants,
        },
        "branding": event.branding or {},
        "jeopardy_enabled": event.jeopardy_enabled,
        "ad_enabled": event.ad_enabled,
        "my_registration": registration.status if registration else None,
    }


def serialize_admin(event):
    data = serialize_public(event)
    data.update(
        {
            "description": event.description,
            "rules": event.rules,
            "registration_open": event.registration_open,
            "registration_opens_at": v.iso(event.registration_opens_at),
            "registration_closes_at": v.iso(event.registration_closes_at),
            "requires_approval": event.requires_approval,
            "max_participants": event.max_participants,
            "seat_count": event.seat_count,
            "settings": event_settings.effective(event),
            "sealed_module_id": event.sealed_module_id,
            "created_at": v.iso(event.created_at),
            "updated_at": v.iso(event.updated_at),
            "allowed_transitions": sorted(TRANSITIONS[event.status]),
        }
    )
    return data


def registration_window_open(event, now=None):
    now = now or utcnow()
    if not event.registration_open:
        return False
    if effective_phase(event, now) not in ("upcoming", "active"):
        return False
    if event.registration_opens_at and now < event.registration_opens_at:
        return False
    closes = event.registration_closes_at or event.ends_at
    if closes and now >= closes:
        return False
    return True

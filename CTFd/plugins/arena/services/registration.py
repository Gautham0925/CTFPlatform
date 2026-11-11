"""Event registration service.

Capacity is enforced with a conditional atomic UPDATE on
``arena_events.seat_count`` in the same transaction as the registration row:

    UPDATE arena_events SET seat_count = seat_count + 1
     WHERE id = :id AND (max_participants IS NULL OR seat_count < max_participants)

Exactly one statement can take the last seat; the others affect 0 rows and are
refused. A unique (event_id, user_id) constraint makes duplicate registration
impossible even under concurrent requests. Invitation-code use is limited the
same way.
"""

import datetime
import hashlib
import secrets

from sqlalchemy import and_, or_, update
from sqlalchemy.exc import IntegrityError

from CTFd.models import Users, db
from CTFd.plugins.arena.api import ApiError
from CTFd.plugins.arena.models import (
    SEAT_STATUSES,
    ArenaEvent,
    ArenaEventInvite,
    ArenaRegistration,
)
from CTFd.plugins.arena.services import events as event_service
from CTFd.plugins.arena.services import settings as event_settings
from CTFd.utils import get_config

# Hooks called after a registration stops being active (teams leave, etc).
DEACTIVATE_HOOKS = []


def hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().encode()).hexdigest()


def new_code() -> str:
    # 20 base32-ish chars, ~100 bits of entropy.
    return secrets.token_urlsafe(15)


def _now():
    return datetime.datetime.utcnow()


# ------------------------------------------------------------------- seats


def _take_seat(event_id):
    t = ArenaEvent.__table__
    result = db.session.execute(
        update(t)
        .where(
            and_(
                t.c.id == event_id,
                or_(
                    t.c.max_participants.is_(None),
                    t.c.seat_count < t.c.max_participants,
                ),
            )
        )
        .values(seat_count=t.c.seat_count + 1)
    )
    return result.rowcount == 1


def _release_seat(event_id):
    t = ArenaEvent.__table__
    db.session.execute(
        update(t)
        .where(and_(t.c.id == event_id, t.c.seat_count > 0))
        .values(seat_count=t.c.seat_count - 1)
    )


def _use_invite(invite_id):
    t = ArenaEventInvite.__table__
    now = _now()
    result = db.session.execute(
        update(t)
        .where(
            and_(
                t.c.id == invite_id,
                t.c.revoked_at.is_(None),
                or_(t.c.expires_at.is_(None), t.c.expires_at > now),
                or_(t.c.max_uses.is_(None), t.c.uses < t.c.max_uses),
            )
        )
        .values(uses=t.c.uses + 1)
    )
    return result.rowcount == 1


def _cas_status(reg_id, old_status, values):
    """Compare-and-set a registration's status; False if it changed meanwhile."""
    t = ArenaRegistration.__table__
    values = dict(values, updated_at=_now())
    result = db.session.execute(
        update(t)
        .where(and_(t.c.id == reg_id, t.c.status == old_status))
        .values(**values)
    )
    return result.rowcount == 1


# ------------------------------------------------------------- eligibility


def _check_eligibility(event, user, admin=False):
    if user.banned:
        raise ApiError(403, "Your account is banned", code="banned")
    s = event_settings.effective(event)
    if (
        s["require_verified_email"] or get_config("verify_emails")
    ) and not user.verified:
        raise ApiError(403, "Verify your email address first", code="unverified")
    domains = s["allowed_email_domains"]
    if domains and not admin:
        email_domain = (user.email or "").rsplit("@", 1)[-1].lower()
        if email_domain not in domains:
            raise ApiError(403, "Your email domain is not eligible", code="ineligible")


def find_invite_by_code(code):
    if not code or not isinstance(code, str) or len(code) > 128:
        return None
    return ArenaEventInvite.query.filter_by(code_hash=hash_code(code)).first()


def _invite_usable(invite, now=None):
    now = now or _now()
    if invite.revoked_at is not None:
        return False
    if invite.expires_at is not None and invite.expires_at <= now:
        return False
    if invite.max_uses is not None and invite.uses >= invite.max_uses:
        return False
    return True


# -------------------------------------------------------------- register


def register(event, user, invite_code=None):
    """Self-service registration. Returns the registration."""
    if not event_service.registration_window_open(event):
        raise ApiError(409, "Registration is closed", code="registration_closed")
    _check_eligibility(event, user)

    s = event_settings.effective(event)
    invite = None
    if event.visibility == "private" or s["invite_only"] or invite_code:
        invite = find_invite_by_code(invite_code) if invite_code else None
        if invite is None or invite.event_id != event.id or not _invite_usable(invite):
            raise ApiError(
                403, "A valid invitation code is required", code="invite_required"
            )

    existing = ArenaRegistration.query.filter_by(
        event_id=event.id, user_id=user.id
    ).first()
    if existing is not None:
        if existing.status in SEAT_STATUSES:
            raise ApiError(409, "You are already registered", code="already_registered")
        if existing.status in ("rejected", "banned"):
            raise ApiError(
                403, "You cannot register for this event", code="not_allowed"
            )

    status = "pending" if event.requires_approval else "approved"
    try:
        if invite is not None and not _use_invite(invite.id):
            db.session.rollback()
            raise ApiError(
                403, "Invitation code is no longer valid", code="invite_required"
            )
        if not _take_seat(event.id):
            db.session.rollback()
            raise ApiError(409, "This event is full", code="event_full")
        if existing is not None:  # re-registration after withdrawal
            changed = _cas_status(
                existing.id,
                existing.status,
                {
                    "status": status,
                    "source": "self",
                    "invite_id": invite.id if invite else None,
                },
            )
            if not changed:
                db.session.rollback()
                raise ApiError(
                    409, "Registration changed concurrently; retry", code="conflict"
                )
            reg = existing
        else:
            reg = ArenaRegistration(
                event_id=event.id,
                user_id=user.id,
                status=status,
                source="self",
                invite_id=invite.id if invite else None,
            )
            db.session.add(reg)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # also rolls back the seat
        raise ApiError(409, "You are already registered", code="already_registered")
    db.session.refresh(event)
    db.session.refresh(reg)
    return reg


def withdraw(event, user):
    reg = ArenaRegistration.query.filter_by(event_id=event.id, user_id=user.id).first()
    if reg is None or reg.status not in SEAT_STATUSES:
        raise ApiError(404, "You are not registered")
    _set_status(event, reg, "withdrawn")
    return reg


# ----------------------------------------------------------------- admin


ADMIN_ACTIONS = {
    "approve": ({"pending"}, "approved"),
    "reject": ({"pending"}, "rejected"),
    "ban": ({"pending", "approved", "withdrawn", "rejected"}, "banned"),
    "unban": ({"banned"}, "approved"),
    "remove": ({"pending", "approved"}, "withdrawn"),
}


def _set_status(event, reg, new_status, actor=None):
    old_status = reg.status
    held = old_status in SEAT_STATUSES
    will_hold = new_status in SEAT_STATUSES
    values = {"status": new_status}
    if new_status == "approved" and actor is not None:
        values["approved_by"] = actor.id
    # CAS first: a concurrent change of the same row makes this a no-op, so
    # the seat counter can never be moved twice for one transition.
    if not _cas_status(reg.id, old_status, values):
        db.session.rollback()
        raise ApiError(409, "Registration changed concurrently; retry", code="conflict")
    if will_hold and not held:
        if not _take_seat(event.id):
            db.session.rollback()
            raise ApiError(409, "This event is full", code="event_full")
    elif held and not will_hold:
        _release_seat(event.id)
    db.session.commit()
    db.session.refresh(reg)
    if held and not will_hold:
        for hook in DEACTIVATE_HOOKS:
            hook(event, reg)
    return reg


def admin_action(event, reg, action, actor):
    if action not in ADMIN_ACTIONS:
        raise ApiError(400, f"Unknown action: {action}")
    allowed_from, to = ADMIN_ACTIONS[action]
    if reg.status not in allowed_from:
        raise ApiError(409, f"Cannot {action} a {reg.status} registration")
    return _set_status(event, reg, to, actor=actor)


def admin_enroll(event, user_id, actor, force=False):
    """Organizer enrollment. Respects capacity unless force=True."""
    user = Users.query.filter_by(id=user_id).first()
    if user is None:
        raise ApiError(404, "User not found")
    _check_eligibility(event, user, admin=True)
    existing = ArenaRegistration.query.filter_by(
        event_id=event.id, user_id=user.id
    ).first()
    if existing is not None and existing.status in SEAT_STATUSES:
        if existing.status == "pending":
            return _set_status(event, existing, "approved", actor=actor)
        raise ApiError(409, "User is already registered", code="already_registered")
    try:
        if force:
            t = ArenaEvent.__table__
            db.session.execute(
                update(t)
                .where(t.c.id == event.id)
                .values(seat_count=t.c.seat_count + 1)
            )
        elif not _take_seat(event.id):
            db.session.rollback()
            raise ApiError(
                409, "This event is full (use force to override)", code="event_full"
            )
        if existing is not None:
            if not _cas_status(
                existing.id,
                existing.status,
                {"status": "approved", "source": "admin", "approved_by": actor.id},
            ):
                db.session.rollback()
                raise ApiError(
                    409, "Registration changed concurrently; retry", code="conflict"
                )
            reg = existing
        else:
            reg = ArenaRegistration(
                event_id=event.id,
                user_id=user.id,
                status="approved",
                source="admin",
                approved_by=actor.id,
            )
            db.session.add(reg)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError(409, "User is already registered", code="already_registered")
    db.session.refresh(reg)
    return reg


def create_invite(event, actor, label=None, max_uses=None, expires_at=None):
    code = new_code()
    invite = ArenaEventInvite(
        event_id=event.id,
        code_hash=hash_code(code),
        label=label,
        max_uses=max_uses,
        uses=0,
        expires_at=expires_at,
        created_by=actor.id if actor else None,
    )
    db.session.add(invite)
    db.session.commit()
    return invite, code  # plaintext code is returned exactly once


def revoke_invite(invite):
    invite.revoked_at = _now()
    db.session.commit()
    return invite


def recount_seats(event_id):
    return ArenaRegistration.query.filter(
        ArenaRegistration.event_id == event_id,
        ArenaRegistration.status.in_(SEAT_STATUSES),
    ).count()


def serialize(reg, include_user=False):
    from CTFd.plugins.arena.validation import iso

    data = {
        "id": reg.id,
        "event_id": reg.event_id,
        "user_id": reg.user_id,
        "status": reg.status,
        "source": reg.source,
        "created_at": iso(reg.created_at),
        "updated_at": iso(reg.updated_at),
    }
    if include_user:
        user = Users.query.filter_by(id=reg.user_id).first()
        data["user"] = (
            {"id": user.id, "name": user.name, "email": user.email} if user else None
        )
    return data


def serialize_invite(invite):
    from CTFd.plugins.arena.validation import iso

    return {
        "id": invite.id,
        "label": invite.label,
        "max_uses": invite.max_uses,
        "uses": invite.uses,
        "expires_at": iso(invite.expires_at),
        "revoked": invite.revoked_at is not None,
        "usable": _invite_usable(invite),
        "created_at": iso(invite.created_at),
    }

"""Event team service (Phase 3).

Invariants (enforced by DB constraints + compare-and-set updates):
* a user is in at most one team per event (UNIQUE(event_id, user_id));
* team.member_count == number of member rows and never exceeds
  event.team_size_max (conditional atomic UPDATE);
* a team always has exactly one captain while it has members;
* only approved registrants of the event can be members.
"""

import datetime
import hashlib
import secrets

from sqlalchemy import and_, update
from sqlalchemy.exc import IntegrityError

from CTFd.models import Users, db
from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.api import ApiError
from CTFd.plugins.arena.models import (
    TEAM_VISIBILITIES,
    ArenaRegistration,
    ArenaTeam,
    ArenaTeamInvite,
    ArenaTeamMember,
)
from CTFd.plugins.arena.services import events as event_service
from CTFd.plugins.arena.services import settings as event_settings

INVITE_TTL = datetime.timedelta(days=7)
# Hooks deciding whether a team with history may be hard-deleted on disband.
HAS_HISTORY_HOOKS = []


def _now():
    return datetime.datetime.utcnow()


def _hash(code):
    return hashlib.sha256(code.strip().encode()).hexdigest()


def new_join_code():
    return secrets.token_urlsafe(12)


# ------------------------------------------------------------------ lookups


def membership(event, user_id):
    return ArenaTeamMember.query.filter_by(event_id=event.id, user_id=user_id).first()


def team_of(event, user_id):
    m = membership(event, user_id)
    if m is None:
        return None
    return ArenaTeam.query.filter_by(id=m.team_id, event_id=event.id).first()


def get_team(event, team_id):
    return ArenaTeam.query.filter_by(id=team_id, event_id=event.id).first()


def members(team):
    return (
        ArenaTeamMember.query.filter_by(team_id=team.id, event_id=team.event_id)
        .order_by(ArenaTeamMember.joined_at, ArenaTeamMember.id)
        .all()
    )


# ------------------------------------------------------------------- checks


def _require_team_mode(event):
    if event.team_mode != "team":
        raise ApiError(409, "This event is not a team event", code="not_team_event")


def membership_locked(event):
    phase = event_service.effective_phase(event)
    if phase in ("completed", "archived"):
        return True
    if event_settings.get(event, "lock_teams_after_start") and phase in (
        "active",
        "paused",
    ):
        return True
    return False


def _require_unlocked(event):
    if membership_locked(event):
        raise ApiError(
            409, "Team membership is locked for this event", code="teams_locked"
        )


def _require_approved(event, user_id):
    reg = ArenaRegistration.query.filter_by(event_id=event.id, user_id=user_id).first()
    if reg is None or reg.status != "approved":
        raise ApiError(
            403,
            "User is not an approved participant of this event",
            code="not_registered",
        )


def _require_active(team):
    if team.status != "active":
        raise ApiError(409, f"Team is {team.status}", code=f"team_{team.status}")


def _require_free_to_move(event, user_id):
    """Members of a banned team cannot leave it to evade the ban."""
    t = team_of(event, user_id)
    if t is not None and t.status == "banned":
        raise ApiError(403, "Your team is banned", code="team_banned")


def _validate_name(data, required):
    name = v.string(
        data, "name", required=required, max_len=64, min_len=2, allow_empty=False
    )
    if name is not None and any(ord(ch) < 32 for ch in name):
        raise v.ValidationError("name", "must not contain control characters")
    return name


# --------------------------------------------------------------- mutations


def _increment(team_id, max_size):
    t = ArenaTeam.__table__
    res = db.session.execute(
        update(t)
        .where(
            and_(t.c.id == team_id, t.c.status == "active", t.c.member_count < max_size)
        )
        .values(member_count=t.c.member_count + 1)
    )
    return res.rowcount == 1


def _decrement(team_id):
    t = ArenaTeam.__table__
    db.session.execute(
        update(t)
        .where(and_(t.c.id == team_id, t.c.member_count > 0))
        .values(member_count=t.c.member_count - 1)
    )


def create_team(event, user, data):
    _require_team_mode(event)
    _require_unlocked(event)
    _require_approved(event, user.id)
    _require_free_to_move(event, user.id)
    if membership(event, user.id) is not None:
        raise ApiError(
            409, "You are already in a team for this event", code="already_in_team"
        )
    name = _validate_name(data, required=True)
    visibility = v.choice(data, "visibility", TEAM_VISIBILITIES) or event_settings.get(
        event, "default_team_visibility"
    )
    code = new_join_code()
    team = ArenaTeam(
        event_id=event.id,
        name=name,
        captain_user_id=user.id,
        visibility=visibility,
        status="active",
        member_count=1,
        join_code_hash=_hash(code),
        affiliation=v.string(data, "affiliation", max_len=128),
        country=v.string(data, "country", max_len=32),
    )
    try:
        db.session.add(team)
        db.session.flush()
        db.session.add(
            ArenaTeamMember(
                event_id=event.id, team_id=team.id, user_id=user.id, role="captain"
            )
        )
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        if "uq_arena_members_event_user" in str(e.orig) or "arena_team_members" in str(
            e.orig
        ):
            raise ApiError(
                409, "You are already in a team for this event", code="already_in_team"
            )
        raise ApiError(409, "A team with this name already exists", code="name_taken")
    return team, code


def _add_member(event, team, user_id, role="member", force=False):
    """Insert membership atomically. Caller has validated eligibility."""
    max_size = 10**6 if force else event.team_size_max
    try:
        if not _increment(team.id, max_size):
            db.session.rollback()
            db.session.refresh(team)
            if team.status != "active":
                raise ApiError(
                    409, f"Team is {team.status}", code=f"team_{team.status}"
                )
            raise ApiError(409, "Team is full", code="team_full")
        db.session.add(
            ArenaTeamMember(
                event_id=event.id, team_id=team.id, user_id=user_id, role=role
            )
        )
        # Any other pending invites for this user in this event are void now.
        _void_pending_invites(event, user_id)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError(
            409, "User is already in a team for this event", code="already_in_team"
        )


def _void_pending_invites(event, user_id):
    t = ArenaTeamInvite.__table__
    db.session.execute(
        update(t)
        .where(
            and_(
                t.c.event_id == event.id,
                t.c.user_id == user_id,
                t.c.status == "pending",
            )
        )
        .values(status="cancelled", pending_key=None, responded_at=_now())
    )


def join_with_code(event, user, code):
    _require_team_mode(event)
    _require_unlocked(event)
    _require_approved(event, user.id)
    _require_free_to_move(event, user.id)
    if not isinstance(code, str) or not code or len(code) > 64:
        raise ApiError(403, "Invalid join code", code="invalid_code")
    team = ArenaTeam.query.filter_by(
        event_id=event.id, join_code_hash=_hash(code)
    ).first()
    if team is None:  # also covers codes of other events' teams
        raise ApiError(403, "Invalid join code", code="invalid_code")
    _require_active(team)
    if membership(event, user.id) is not None:
        raise ApiError(
            409, "You are already in a team for this event", code="already_in_team"
        )
    _add_member(event, team, user.id)
    return team


def regenerate_join_code(team):
    code = new_join_code()
    team.join_code_hash = _hash(code)
    db.session.commit()
    return code


def _require_captain(event, user):
    team = team_of(event, user.id)
    if team is None:
        raise ApiError(404, "You are not in a team", code="no_team")
    if team.captain_user_id != user.id:
        raise ApiError(403, "Only the team captain can do this", code="not_captain")
    return team


def update_team(event, user, data):
    team = _require_captain(event, user)
    _require_active(team)
    if "name" in data:
        _require_unlocked(event)
        team.name = _validate_name(data, required=True)
    if "visibility" in data:
        team.visibility = v.choice(data, "visibility", TEAM_VISIBILITIES)
    for f, n in (("affiliation", 128), ("country", 32)):
        if f in data:
            setattr(team, f, v.string(data, f, max_len=n))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError(409, "A team with this name already exists", code="name_taken")
    return team


def transfer_captain(event, team, new_captain_id, admin=False):
    m = ArenaTeamMember.query.filter_by(
        event_id=event.id, team_id=team.id, user_id=new_captain_id
    ).first()
    if m is None:
        raise ApiError(404, "User is not a member of this team")
    old = team.captain_user_id
    t = ArenaTeam.__table__
    # CAS on captain id: concurrent transfers cannot both succeed.
    res = db.session.execute(
        update(t)
        .where(and_(t.c.id == team.id, t.c.captain_user_id == old))
        .values(captain_user_id=new_captain_id)
    )
    if res.rowcount != 1:
        db.session.rollback()
        raise ApiError(409, "Team changed concurrently; retry", code="conflict")
    ArenaTeamMember.query.filter_by(team_id=team.id, role="captain").update(
        {"role": "member"}
    )
    m.role = "captain"
    db.session.commit()
    db.session.refresh(team)
    return team


def _remove_member(event, team, user_id, allow_captain_succession=False):
    m = ArenaTeamMember.query.filter_by(
        event_id=event.id, team_id=team.id, user_id=user_id
    ).first()
    if m is None:
        raise ApiError(404, "User is not a member of this team")
    others = [x for x in members(team) if x.user_id != user_id]
    if m.role == "captain" and others:
        if not allow_captain_succession:
            raise ApiError(
                409, "Transfer captaincy before leaving", code="captain_must_transfer"
            )
        successor = others[0]  # longest-standing member
        team.captain_user_id = successor.user_id
        successor.role = "captain"
    deleted = ArenaTeamMember.query.filter_by(id=m.id).delete()
    if deleted:
        _decrement(team.id)
    if not others:
        team.captain_user_id = None
        _disband_in_txn(team)
    db.session.commit()


def leave_team(event, user):
    _require_team_mode(event)
    _require_unlocked(event)
    team = team_of(event, user.id)
    if team is None:
        raise ApiError(404, "You are not in a team", code="no_team")
    if team.status == "banned":
        raise ApiError(403, "Your team is banned", code="team_banned")
    _remove_member(event, team, user.id)


def kick_member(event, captain, user_id):
    team = _require_captain(event, captain)
    _require_unlocked(event)
    if user_id == captain.id:
        raise ApiError(400, "Use leave instead")
    _remove_member(event, team, user_id)
    return team


def _disband_in_txn(team):
    """Delete a team without history; otherwise keep it as 'disbanded'."""
    has_history = any(hook(team) for hook in HAS_HISTORY_HOOKS)
    t = ArenaTeamInvite.__table__
    db.session.execute(
        update(t)
        .where(and_(t.c.team_id == team.id, t.c.status == "pending"))
        .values(status="cancelled", pending_key=None, responded_at=_now())
    )
    if has_history:
        team.status = "disbanded"
        team.join_code_hash = None
        # Free the name for new teams while keeping history readable.
        team.name = f"{team.name[:40]} (disbanded #{team.id})"
    else:
        db.session.delete(team)


def disband(event, team):
    for m in members(team):
        db.session.delete(m)
    team.member_count = 0
    team.captain_user_id = None
    _disband_in_txn(team)
    db.session.commit()


# ------------------------------------------------------------------ invites


def invite(event, captain, user_id):
    team = _require_captain(event, captain)
    _require_active(team)
    _require_unlocked(event)
    _require_approved(event, user_id)
    if membership(event, user_id) is not None:
        raise ApiError(
            409, "User is already in a team for this event", code="already_in_team"
        )
    if team.member_count >= event.team_size_max:
        raise ApiError(409, "Team is full", code="team_full")
    inv = ArenaTeamInvite(
        event_id=event.id,
        team_id=team.id,
        user_id=user_id,
        invited_by=captain.id,
        status="pending",
        pending_key=f"{team.id}:{user_id}",
        expires_at=_now() + INVITE_TTL,
    )
    try:
        db.session.add(inv)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError(
            409,
            "User already has a pending invite to this team",
            code="already_invited",
        )
    return inv


def _resolve_invite(inv, new_status):
    t = ArenaTeamInvite.__table__
    res = db.session.execute(
        update(t)
        .where(and_(t.c.id == inv.id, t.c.status == "pending"))
        .values(status=new_status, pending_key=None, responded_at=_now())
    )
    return res.rowcount == 1


def respond_invite(event, user, invite_id, accept):
    inv = ArenaTeamInvite.query.filter_by(
        id=invite_id, event_id=event.id, user_id=user.id
    ).first()
    if inv is None:
        raise ApiError(404, "Invite not found")
    if inv.status != "pending":
        raise ApiError(409, f"Invite is {inv.status}")
    if inv.expires_at and inv.expires_at <= _now():
        _resolve_invite(inv, "expired")
        db.session.commit()
        raise ApiError(409, "Invite has expired", code="invite_expired")
    team = get_team(event, inv.team_id)
    if not accept:
        if not _resolve_invite(inv, "rejected"):
            db.session.rollback()
            raise ApiError(409, "Invite already handled")
        db.session.commit()
        return None
    _require_unlocked(event)
    _require_approved(event, user.id)
    _require_free_to_move(event, user.id)
    _require_active(team)
    if membership(event, user.id) is not None:
        raise ApiError(
            409, "You are already in a team for this event", code="already_in_team"
        )
    if not _resolve_invite(inv, "accepted"):
        db.session.rollback()
        raise ApiError(409, "Invite already handled")
    _add_member(event, team, user.id)  # commits (or rolls back both)
    return team


def cancel_invite(event, captain, invite_id):
    team = _require_captain(event, captain)
    inv = ArenaTeamInvite.query.filter_by(
        id=invite_id, event_id=event.id, team_id=team.id
    ).first()
    if inv is None:
        raise ApiError(404, "Invite not found")
    if not _resolve_invite(inv, "cancelled"):
        db.session.rollback()
        raise ApiError(409, "Invite already handled")
    db.session.commit()


# ----------------------------------------------------------------- admin ops


def admin_set_status(event, team, status):
    if status not in ("active", "banned"):
        raise ApiError(400, "status must be active or banned")
    if team.status == "disbanded":
        raise ApiError(409, "Team is disbanded")
    team.status = status
    db.session.commit()
    return team


def admin_add_member(event, team, user_id, force=False):
    _require_team_mode(event)
    _require_approved(event, user_id)
    _require_active(team)
    role = "captain" if team.member_count == 0 else "member"
    _add_member(event, team, user_id, role=role, force=force)
    if role == "captain":
        team.captain_user_id = user_id
        db.session.commit()
    return team


def on_registration_deactivated(event, reg):
    """Registration withdrawn/banned/removed: leave the team (no lock checks)."""
    team = team_of(event, reg.user_id)
    if team is None:
        return
    _remove_member(event, team, reg.user_id, allow_captain_succession=True)


# ------------------------------------------------------------ serialization


def serialize(team, include_members=False, private=False):
    data = {
        "id": team.id,
        "event_id": team.event_id,
        "name": team.name,
        "visibility": team.visibility,
        "status": team.status,
        "member_count": team.member_count,
        "affiliation": team.affiliation,
        "country": team.country,
        "captain_user_id": team.captain_user_id,
        "created_at": v.iso(team.created_at),
    }
    if include_members:
        rows = members(team)
        names = dict(
            Users.query.with_entities(Users.id, Users.name)
            .filter(Users.id.in_([m.user_id for m in rows] or [0]))
            .all()
        )
        data["members"] = [
            {
                "user_id": m.user_id,
                "name": names.get(m.user_id),
                "role": m.role,
                "joined_at": v.iso(m.joined_at),
            }
            for m in rows
        ]
    if private:
        data["pending_invites"] = [
            serialize_invite(i)
            for i in ArenaTeamInvite.query.filter_by(team_id=team.id, status="pending")
        ]
    return data


def serialize_invite(inv):
    team = ArenaTeam.query.filter_by(id=inv.team_id).first()
    return {
        "id": inv.id,
        "team_id": inv.team_id,
        "team_name": team.name if team else None,
        "user_id": inv.user_id,
        "status": inv.status,
        "expires_at": v.iso(inv.expires_at),
        "created_at": v.iso(inv.created_at),
    }


def recount(team):
    return ArenaTeamMember.query.filter_by(team_id=team.id).count()

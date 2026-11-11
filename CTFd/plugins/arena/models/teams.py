"""Event-scoped teams (Phase 3). Native Users.team_id is never used."""

from CTFd.models import db
from CTFd.plugins.arena.models.core import UTCDateTime, _in, utcnow

TEAM_STATUSES = ("active", "banned", "disbanded")
TEAM_VISIBILITIES = ("public", "private")
MEMBER_ROLES = ("captain", "member")
INVITE_STATUSES = ("pending", "accepted", "rejected", "cancelled", "expired")


class ArenaTeam(db.Model):
    __tablename__ = "arena_teams"
    __table_args__ = (
        db.UniqueConstraint("event_id", "name", name="uq_arena_teams_event_name"),
        db.UniqueConstraint("join_code_hash", name="uq_arena_teams_join_code"),
        db.CheckConstraint(_in("status", TEAM_STATUSES), name="ck_arena_teams_status"),
        db.CheckConstraint(
            _in("visibility", TEAM_VISIBILITIES), name="ck_arena_teams_visibility"
        ),
        db.CheckConstraint("member_count >= 0", name="ck_arena_teams_member_count"),
        db.Index("ix_arena_teams_event_status", "event_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_teams_event"
        ),
        nullable=False,
    )
    name = db.Column(db.String(64), nullable=False)
    captain_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_teams_captain"),
        nullable=True,
    )
    visibility = db.Column(db.String(16), nullable=False, default="public")
    status = db.Column(db.String(16), nullable=False, default="active")
    member_count = db.Column(db.Integer, nullable=False, default=0)
    join_code_hash = db.Column(db.String(64), nullable=True)
    affiliation = db.Column(db.String(128), nullable=True)
    country = db.Column(db.String(32), nullable=True)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)
    updated_at = db.Column(UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow)


class ArenaTeamMember(db.Model):
    __tablename__ = "arena_team_members"
    __table_args__ = (
        # One team per user per event: the core multi-event team guarantee.
        db.UniqueConstraint("event_id", "user_id", name="uq_arena_members_event_user"),
        db.CheckConstraint(_in("role", MEMBER_ROLES), name="ck_arena_members_role"),
        db.Index("ix_arena_members_team", "team_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_members_event"
        ),
        nullable=False,
    )
    team_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_teams.id", ondelete="CASCADE", name="fk_arena_members_team"
        ),
        nullable=False,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE", name="fk_arena_members_user"),
        nullable=False,
    )
    role = db.Column(db.String(16), nullable=False, default="member")
    joined_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class ArenaTeamInvite(db.Model):
    __tablename__ = "arena_team_invites"
    __table_args__ = (
        # Set while pending, NULL afterwards: at most one pending invite per
        # (team, user) without relying on partial indexes.
        db.UniqueConstraint("pending_key", name="uq_arena_tinv_pending"),
        db.CheckConstraint(_in("status", INVITE_STATUSES), name="ck_arena_tinv_status"),
        db.Index("ix_arena_tinv_user_status", "user_id", "status"),
        db.Index("ix_arena_tinv_team", "team_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_tinv_event"
        ),
        nullable=False,
    )
    team_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_teams.id", ondelete="CASCADE", name="fk_arena_tinv_team"),
        nullable=False,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE", name="fk_arena_tinv_user"),
        nullable=False,
    )
    invited_by = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_tinv_inviter"),
        nullable=True,
    )
    status = db.Column(db.String(16), nullable=False, default="pending")
    pending_key = db.Column(db.String(32), nullable=True)
    expires_at = db.Column(UTCDateTime, nullable=True)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)
    responded_at = db.Column(UTCDateTime, nullable=True)

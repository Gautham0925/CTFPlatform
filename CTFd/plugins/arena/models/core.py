"""Arena-owned models (Phase 1: events, registrations, roles).

No native CTFd table is altered. Enumerated values are stored as short strings
and enforced both in Python (@validates) and in the database (CHECK).
Constraint names are explicit so that migrations stay reversible.
"""

import datetime

from sqlalchemy.dialects import mysql
from sqlalchemy.orm import validates

from CTFd.models import db

EVENT_TYPES = ("jeopardy", "ad", "mixed")
EVENT_STATUSES = ("draft", "upcoming", "active", "paused", "completed", "archived")
EVENT_VISIBILITIES = ("public", "unlisted", "private")
TEAM_MODES = ("individual", "team")

REGISTRATION_STATUSES = ("pending", "approved", "rejected", "withdrawn", "banned")
REGISTRATION_SOURCES = ("self", "admin", "import")

ROLE_ORGANIZER = "organizer"
ROLE_CHALLENGE_MANAGER = "challenge_manager"
ROLE_SCOREBOARD_OPERATOR = "scoreboard_operator"
ROLE_AD_OPERATOR = "ad_operator"
ROLE_CERT_MANAGER = "cert_manager"
ROLE_ANALYST = "analyst"
ROLES = (
    ROLE_ORGANIZER,
    ROLE_CHALLENGE_MANAGER,
    ROLE_SCOREBOARD_OPERATOR,
    ROLE_AD_OPERATOR,
    ROLE_CERT_MANAGER,
    ROLE_ANALYST,
)
# Statuses of a registration that occupy a seat (count towards capacity).
SEAT_STATUSES = ("pending", "approved")

# Microsecond precision on MySQL/MariaDB keeps ordering stable for bursts.
UTCDateTime = db.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def utcnow():
    return datetime.datetime.utcnow()


def _in(column, values):
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _check(value, allowed, field):
    if value not in allowed:
        raise ValueError(f"Invalid {field}: {value!r}")
    return value


class ArenaEvent(db.Model):
    __tablename__ = "arena_events"
    __table_args__ = (
        db.CheckConstraint(_in("type", EVENT_TYPES), name="ck_arena_events_type"),
        db.CheckConstraint(
            _in("status", EVENT_STATUSES), name="ck_arena_events_status"
        ),
        db.CheckConstraint(
            _in("visibility", EVENT_VISIBILITIES), name="ck_arena_events_visibility"
        ),
        db.CheckConstraint(
            _in("team_mode", TEAM_MODES), name="ck_arena_events_team_mode"
        ),
        db.UniqueConstraint("slug", name="uq_arena_events_slug"),
        db.UniqueConstraint("sealed_module_id", name="uq_arena_events_sealed_module"),
        db.Index("ix_arena_events_status_starts", "status", "starts_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(64), nullable=False)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.Text)
    rules = db.Column(db.Text)

    type = db.Column(db.String(16), nullable=False, default="jeopardy")
    status = db.Column(db.String(16), nullable=False, default="draft")
    visibility = db.Column(db.String(16), nullable=False, default="public")

    starts_at = db.Column(UTCDateTime, nullable=True)
    ends_at = db.Column(UTCDateTime, nullable=True)
    freeze_at = db.Column(UTCDateTime, nullable=True)

    registration_open = db.Column(db.Boolean, nullable=False, default=False)
    registration_opens_at = db.Column(UTCDateTime, nullable=True)
    registration_closes_at = db.Column(UTCDateTime, nullable=True)
    requires_approval = db.Column(db.Boolean, nullable=False, default=False)
    max_participants = db.Column(db.Integer, nullable=True)

    team_mode = db.Column(db.String(16), nullable=False, default="individual")
    team_size_min = db.Column(db.Integer, nullable=False, default=1)
    team_size_max = db.Column(db.Integer, nullable=False, default=1)

    # Branding and settings are validated by services.events / services.settings.
    branding = db.Column(db.JSON, nullable=True)
    settings = db.Column(db.JSON, nullable=True)
    # Atomic capacity counter: seats held by pending/approved registrations.
    seat_count = db.Column(db.Integer, nullable=False, default=0)

    jeopardy_enabled = db.Column(db.Boolean, nullable=False, default=True)
    ad_enabled = db.Column(db.Boolean, nullable=False, default=False)

    # Native module used as an isolation wall (no audience => native 404).
    # Created by event management in Phase 2.
    sealed_module_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "modules.id", ondelete="RESTRICT", name="fk_arena_events_sealed_module"
        ),
        nullable=True,
    )

    created_by = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_events_creator"),
        nullable=True,
    )
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)
    updated_at = db.Column(UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow)

    @validates("type")
    def _v_type(self, key, value):
        return _check(value, EVENT_TYPES, key)

    @validates("status")
    def _v_status(self, key, value):
        return _check(value, EVENT_STATUSES, key)

    @validates("visibility")
    def _v_visibility(self, key, value):
        return _check(value, EVENT_VISIBILITIES, key)

    @validates("team_mode")
    def _v_team_mode(self, key, value):
        return _check(value, TEAM_MODES, key)

    def __repr__(self):
        return f"<ArenaEvent {self.id} {self.slug!r}>"


class ArenaRegistration(db.Model):
    __tablename__ = "arena_registrations"
    __table_args__ = (
        db.UniqueConstraint("event_id", "user_id", name="uq_arena_reg_event_user"),
        db.CheckConstraint(
            _in("status", REGISTRATION_STATUSES), name="ck_arena_reg_status"
        ),
        db.CheckConstraint(
            _in("source", REGISTRATION_SOURCES), name="ck_arena_reg_source"
        ),
        db.Index("ix_arena_reg_event_status", "event_id", "status"),
        db.Index("ix_arena_reg_user", "user_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_events.id", ondelete="CASCADE", name="fk_arena_reg_event"),
        nullable=False,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE", name="fk_arena_reg_user"),
        nullable=False,
    )
    status = db.Column(db.String(16), nullable=False, default="pending")
    source = db.Column(db.String(16), nullable=False, default="self")
    approved_by = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_reg_approver"),
        nullable=True,
    )
    invite_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_event_invites.id", ondelete="SET NULL", name="fk_arena_reg_invite"
        ),
        nullable=True,
    )
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)
    updated_at = db.Column(UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow)

    @validates("status")
    def _v_status(self, key, value):
        return _check(value, REGISTRATION_STATUSES, key)

    @validates("source")
    def _v_source(self, key, value):
        return _check(value, REGISTRATION_SOURCES, key)


class ArenaRole(db.Model):
    """Scoped administrative role.

    event_id NULL means the role applies to all events. Native CTFd admins are
    superadmins and need no row here. Uniqueness of global roles (NULL
    event_id) is enforced by rbac.grant_role() because SQL UNIQUE treats
    NULLs as distinct.
    """

    __tablename__ = "arena_roles"
    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "event_id", "role", name="uq_arena_roles_user_event_role"
        ),
        db.CheckConstraint(_in("role", ROLES), name="ck_arena_roles_role"),
        db.Index("ix_arena_roles_event", "event_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE", name="fk_arena_roles_user"),
        nullable=False,
    )
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_roles_event"
        ),
        nullable=True,
    )
    role = db.Column(db.String(32), nullable=False)
    granted_by = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_roles_granter"),
        nullable=True,
    )
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)

    @validates("role")
    def _v_role(self, key, value):
        return _check(value, ROLES, key)


class ArenaEventInvite(db.Model):
    """Invitation code for registering into an event (required for private
    events and for events with settings.invite_only). Only a SHA-256 hash of
    the code is stored."""

    __tablename__ = "arena_event_invites"
    __table_args__ = (
        db.UniqueConstraint("code_hash", name="uq_arena_invites_code"),
        db.Index("ix_arena_invites_event", "event_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_invites_event"
        ),
        nullable=False,
    )
    code_hash = db.Column(db.String(64), nullable=False)
    label = db.Column(db.String(128), nullable=True)
    max_uses = db.Column(db.Integer, nullable=True)
    uses = db.Column(db.Integer, nullable=False, default=0)
    expires_at = db.Column(UTCDateTime, nullable=True)
    revoked_at = db.Column(UTCDateTime, nullable=True)
    created_by = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_invites_creator"),
        nullable=True,
    )
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class ArenaAnnouncement(db.Model):
    """Event-scoped announcement (native CTFd notifications are global)."""

    __tablename__ = "arena_announcements"
    __table_args__ = (db.Index("ix_arena_ann_event_time", "event_id", "created_at"),)

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_events.id", ondelete="CASCADE", name="fk_arena_ann_event"),
        nullable=False,
    )
    title = db.Column(db.String(128), nullable=False)
    body = db.Column(db.Text, nullable=False)
    pinned = db.Column(db.Boolean, nullable=False, default=False)
    created_by = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_ann_creator"),
        nullable=True,
    )
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)

"""External lab integration models (Phase 11)."""

from CTFd.models import db
from CTFd.plugins.arena.models.core import UTCDateTime, _in, utcnow

LAB_ADAPTERS = ("mock", "generic_http")
LAB_SESSION_STATUSES = (
    "requested",
    "provisioning",
    "ready",
    "stopping",
    "released",
    "expired",
    "failed",
)
ACTIVE_LAB_STATUSES = ("requested", "provisioning", "ready", "stopping")


class ArenaLabProvider(db.Model):
    __tablename__ = "arena_lab_providers"
    __table_args__ = (
        db.UniqueConstraint("name", name="uq_arena_labp_name"),
        db.CheckConstraint(_in("adapter", LAB_ADAPTERS), name="ck_arena_labp_adapter"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    adapter = db.Column(db.String(32), nullable=False)
    base_url = db.Column(db.String(255), nullable=True)
    credentials_enc = db.Column(
        db.Text, nullable=True
    )  # Fernet token, never returned by APIs
    config = db.Column(db.JSON, nullable=True)  # timeouts etc.
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    last_health = db.Column(db.JSON, nullable=True)
    last_health_at = db.Column(UTCDateTime, nullable=True)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class ArenaEventLab(db.Model):
    """A provider lab template made available in an event."""

    __tablename__ = "arena_event_labs"
    __table_args__ = (
        db.UniqueConstraint(
            "event_id", "provider_id", "template_id", name="uq_arena_elab_template"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_elab_event"
        ),
        nullable=False,
    )
    provider_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_lab_providers.id", ondelete="RESTRICT", name="fk_arena_elab_provider"
        ),
        nullable=False,
    )
    template_id = db.Column(db.String(128), nullable=False)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.Text, nullable=True)
    ttl_minutes = db.Column(db.Integer, nullable=False, default=120)
    max_sessions = db.Column(db.Integer, nullable=False, default=50)
    completion_points = db.Column(db.Integer, nullable=False, default=0)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class ArenaLabSession(db.Model):
    __tablename__ = "arena_lab_sessions"
    __table_args__ = (
        db.UniqueConstraint("uuid", name="uq_arena_labs_uuid"),
        db.UniqueConstraint("active_key", name="uq_arena_labs_active"),
        db.CheckConstraint(
            _in("status", LAB_SESSION_STATUSES), name="ck_arena_labs_status"
        ),
        db.Index("ix_arena_labs_event_entity", "event_id", "entity_key"),
        db.Index("ix_arena_labs_status_exp", "status", "expires_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(36), nullable=False)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_labs_event"
        ),
        nullable=False,
    )
    event_lab_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_event_labs.id", ondelete="CASCADE", name="fk_arena_labs_elab"
        ),
        nullable=False,
    )
    provider_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_lab_providers.id", ondelete="RESTRICT", name="fk_arena_labs_provider"
        ),
        nullable=False,
    )
    entity_key = db.Column(db.String(32), nullable=False)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_labs_user"),
        nullable=True,
    )
    external_id = db.Column(db.String(128), nullable=True)
    status = db.Column(db.String(16), nullable=False, default="requested")
    active_key = db.Column(db.String(96), nullable=True)
    connection_enc = db.Column(db.Text, nullable=True)
    error = db.Column(db.String(255), nullable=True)
    expires_at = db.Column(UTCDateTime, nullable=True)
    completed_at = db.Column(UTCDateTime, nullable=True)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)
    updated_at = db.Column(UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow)

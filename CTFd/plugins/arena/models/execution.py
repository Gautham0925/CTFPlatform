"""Challenge execution: image registry and instances (Phase 8)."""

from CTFd.models import db
from CTFd.plugins.arena.models.core import UTCDateTime, _in, utcnow

INSTANCE_STATUSES = (
    "requested",
    "starting",
    "running",
    "stopping",
    "stopped",
    "failed",
    "expired",
)
ACTIVE_INSTANCE_STATUSES = ("requested", "starting", "running", "stopping", "stopped")


class ArenaImage(db.Model):
    """Allowlisted challenge image, pinned by digest."""

    __tablename__ = "arena_images"
    __table_args__ = (
        db.UniqueConstraint("name", "version", name="uq_arena_images_name_version"),
        db.UniqueConstraint("reference", name="uq_arena_images_reference"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    version = db.Column(db.String(32), nullable=False)
    reference = db.Column(db.String(255), nullable=False)  # repo@sha256:<digest>
    ports = db.Column(db.JSON, nullable=False)  # [80, ...]
    cpu_millis = db.Column(db.Integer, nullable=False, default=500)
    memory_mb = db.Column(db.Integer, nullable=False, default=256)
    pids = db.Column(db.Integer, nullable=False, default=128)
    ttl_seconds = db.Column(db.Integer, nullable=False, default=3600)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    validated_at = db.Column(UTCDateTime, nullable=True)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class ArenaInstance(db.Model):
    __tablename__ = "arena_instances"
    __table_args__ = (
        db.UniqueConstraint("uuid", name="uq_arena_inst_uuid"),
        # Set while active: one active instance per (event, challenge, entity).
        db.UniqueConstraint("active_key", name="uq_arena_inst_active"),
        db.CheckConstraint(
            _in("status", INSTANCE_STATUSES), name="ck_arena_inst_status"
        ),
        db.Index("ix_arena_inst_event_entity", "event_id", "entity_key", "status"),
        db.Index("ix_arena_inst_status_exp", "status", "expires_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(36), nullable=False)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_inst_event"
        ),
        nullable=False,
    )
    event_challenge_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_event_challenges.id", ondelete="SET NULL", name="fk_arena_inst_ec"
        ),
        nullable=True,
    )
    image_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_images.id", ondelete="RESTRICT", name="fk_arena_inst_image"
        ),
        nullable=False,
    )
    entity_key = db.Column(db.String(32), nullable=False)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_inst_user"),
        nullable=True,
    )
    purpose = db.Column(
        db.String(16), nullable=False, default="challenge"
    )  # challenge|ad_service
    status = db.Column(db.String(16), nullable=False, default="requested")
    active_key = db.Column(db.String(96), nullable=True)
    host = db.Column(db.String(255), nullable=True)
    ports = db.Column(db.JSON, nullable=True)
    error = db.Column(db.String(255), nullable=True)
    expires_at = db.Column(UTCDateTime, nullable=True)
    last_status_at = db.Column(UTCDateTime, nullable=True)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)
    updated_at = db.Column(UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow)

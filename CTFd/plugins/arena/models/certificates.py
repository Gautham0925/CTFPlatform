"""Optional certificates (Phase 12). Tables always exist; the feature is
inert unless ARENA_CERTIFICATES_ENABLED=true."""

from CTFd.models import db
from CTFd.plugins.arena.models.core import UTCDateTime, _in, utcnow

CERT_KINDS = ("participation", "winner", "top_n", "team", "custom")
CERT_STATUSES = ("pending", "issued", "failed", "revoked")


class ArenaCertTemplate(db.Model):
    __tablename__ = "arena_cert_templates"
    __table_args__ = (
        db.CheckConstraint(_in("kind", CERT_KINDS), name="ck_arena_ctpl_kind"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_ctpl_event"
        ),
        nullable=False,
    )
    name = db.Column(db.String(128), nullable=False)
    kind = db.Column(db.String(16), nullable=False)
    title = db.Column(
        db.String(128), nullable=False, default="Certificate of Achievement"
    )
    body = db.Column(
        db.String(1000), nullable=False
    )  # placeholders {name} {team} {event} {rank} {date}
    criteria = db.Column(
        db.JSON, nullable=True
    )  # {min_score, min_solves, top_n, user_ids}
    accent_color = db.Column(db.String(7), nullable=False, default="#1f6feb")
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class ArenaCertificate(db.Model):
    __tablename__ = "arena_certificates"
    __table_args__ = (
        db.UniqueConstraint("public_id", name="uq_arena_cert_public"),
        # One certificate per template and recipient ("u:<id>" / "t:<id>").
        db.UniqueConstraint(
            "template_id", "recipient_key", name="uq_arena_cert_recipient"
        ),
        db.CheckConstraint(_in("status", CERT_STATUSES), name="ck_arena_cert_status"),
        db.Index("ix_arena_cert_event_user", "event_id", "user_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(36), nullable=False)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_cert_event"
        ),
        nullable=False,
    )
    template_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_cert_templates.id", ondelete="CASCADE", name="fk_arena_cert_tpl"
        ),
        nullable=False,
    )
    recipient_key = db.Column(db.String(32), nullable=False)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_cert_user"),
        nullable=True,
    )
    team_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_teams.id", ondelete="SET NULL", name="fk_arena_cert_team"),
        nullable=True,
    )
    recipient_name = db.Column(db.String(128), nullable=False)
    team_name = db.Column(db.String(128), nullable=True)
    rank = db.Column(db.Integer, nullable=True)
    score = db.Column(db.Integer, nullable=True)
    kind = db.Column(db.String(16), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="pending")
    file_location = db.Column(db.String(255), nullable=True)
    verify_code = db.Column(db.String(64), nullable=False)
    issued_at = db.Column(UTCDateTime, nullable=True)
    revoked_at = db.Column(UTCDateTime, nullable=True)
    revoke_reason = db.Column(db.String(255), nullable=True)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)

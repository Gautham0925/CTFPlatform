"""Scoring ledger and scoreboard snapshots (Phase 5)."""

from CTFd.models import db
from CTFd.plugins.arena.models.activity import BigInt
from CTFd.plugins.arena.models.core import UTCDateTime, _in, utcnow

SCORE_COMPONENTS = (
    "solve",  # one per accepted Jeopardy solve (value derived, see services.scoring)
    "first_blood",
    "hint",
    "bonus",
    "penalty",
    "ad_attack",
    "ad_defense",
    "ad_sla",
)


class ArenaScoreEvent(db.Model):
    """Append-only scoring ledger. Never updated; corrections are new rows."""

    __tablename__ = "arena_score_events"
    __table_args__ = (
        # Idempotency: the same source can never score twice.
        db.UniqueConstraint(
            "event_id", "source", "source_ref", name="uq_arena_score_source"
        ),
        db.CheckConstraint(
            _in("component", SCORE_COMPONENTS), name="ck_arena_score_component"
        ),
        db.Index("ix_arena_score_event_entity", "event_id", "entity_key"),
        db.Index("ix_arena_score_event_time", "event_id", "created_at"),
    )

    id = db.Column(BigInt, primary_key=True, autoincrement=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_score_event"
        ),
        nullable=False,
    )
    entity_key = db.Column(db.String(32), nullable=False)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_score_user"),
        nullable=True,
    )
    component = db.Column(db.String(16), nullable=False)
    points = db.Column(db.Integer, nullable=False, default=0)
    source = db.Column(db.String(32), nullable=False)
    source_ref = db.Column(db.String(96), nullable=False)
    event_challenge_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_event_challenges.id", ondelete="SET NULL", name="fk_arena_score_ec"
        ),
        nullable=True,
    )
    reason = db.Column(db.String(255), nullable=True)
    actor_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_score_actor"),
        nullable=True,
    )
    config_version = db.Column(db.Integer, nullable=True)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class ArenaScoreboardSnapshot(db.Model):
    """Last-known-good scoreboard per (event, scope); used during outages and
    for score history snapshots (Phase 10)."""

    __tablename__ = "arena_scoreboard_snapshots"

    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_sbsnap_event"
        ),
        primary_key=True,
    )
    scope = db.Column(db.String(32), primary_key=True)
    version = db.Column(db.Integer, nullable=False, default=0)
    payload = db.Column(db.JSON, nullable=False)
    computed_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class ArenaScoringConfig(db.Model):
    """Versioned, immutable scoring configuration (weights) per event.

    A change creates version N+1; older versions are kept for audit and for
    reproducing historical snapshots (Phase 10)."""

    __tablename__ = "arena_scoring_configs"
    __table_args__ = (
        db.UniqueConstraint("event_id", "version", name="uq_arena_scfg_event_version"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_scfg_event"
        ),
        nullable=False,
    )
    version = db.Column(db.Integer, nullable=False)
    params = db.Column(db.JSON, nullable=False)
    reason = db.Column(db.String(255), nullable=True)
    created_by = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_scfg_creator"),
        nullable=True,
    )
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class ArenaScoreSnapshot(db.Model):
    """Historical scoreboard snapshot (audit / history, Phase 10).

    kind: round (A&D round closed), periodic, config_before, config_after,
    final. Payload holds condensed standings [{key, name, score, rank,
    jeopardy, attack_defense, adjustments}]."""

    __tablename__ = "arena_score_snapshots"
    __table_args__ = (db.Index("ix_arena_ssnap_event_time", "event_id", "created_at"),)

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_ssnap_event"
        ),
        nullable=False,
    )
    kind = db.Column(db.String(16), nullable=False)
    ref = db.Column(db.String(64), nullable=True)
    config_version = db.Column(db.Integer, nullable=True)
    payload = db.Column(db.JSON, nullable=False)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)

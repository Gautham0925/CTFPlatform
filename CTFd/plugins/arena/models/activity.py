"""Activity / audit log (admin actions from Phase 2, participant activity Phase 7)."""

from CTFd.models import db
from CTFd.plugins.arena.models.core import UTCDateTime, utcnow

BigInt = db.BigInteger().with_variant(db.Integer(), "sqlite")


class ArenaActivity(db.Model):
    __tablename__ = "arena_activity"
    __table_args__ = (
        db.Index("ix_arena_activity_event_time", "event_id", "created_at"),
        db.Index("ix_arena_activity_actor_time", "actor_user_id", "created_at"),
        db.Index("ix_arena_activity_action_time", "action", "created_at"),
    )

    id = db.Column(BigInt, primary_key=True, autoincrement=True)
    # SET NULL: audit records outlive deleted events and users.
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="SET NULL", name="fk_arena_activity_event"
        ),
        nullable=True,
    )
    actor_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_activity_actor"),
        nullable=True,
    )
    actor_type = db.Column(db.String(16), nullable=False, default="user")
    action = db.Column(db.String(48), nullable=False)
    target_type = db.Column(db.String(32), nullable=True)
    target_id = db.Column(db.String(64), nullable=True)
    meta = db.Column(db.JSON, nullable=True)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class ArenaStatsHourly(db.Model):
    """Per-event hourly rollup (refreshed by the worker, never on request)."""

    __tablename__ = "arena_stats_hourly"

    event_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_events.id", ondelete="CASCADE", name="fk_arena_sth_event"),
        primary_key=True,
    )
    bucket_start = db.Column(UTCDateTime, primary_key=True)
    active_users = db.Column(db.Integer, nullable=False, default=0)
    active_entities = db.Column(db.Integer, nullable=False, default=0)
    submissions = db.Column(db.Integer, nullable=False, default=0)
    correct = db.Column(db.Integer, nullable=False, default=0)
    registrations = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class ArenaStatsChallenge(db.Model):
    __tablename__ = "arena_stats_challenge"

    event_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_events.id", ondelete="CASCADE", name="fk_arena_stc_event"),
        primary_key=True,
    )
    event_challenge_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_event_challenges.id", ondelete="CASCADE", name="fk_arena_stc_ec"
        ),
        primary_key=True,
    )
    attempts = db.Column(db.Integer, nullable=False, default=0)
    incorrect = db.Column(db.Integer, nullable=False, default=0)
    solves = db.Column(db.Integer, nullable=False, default=0)
    unique_attempters = db.Column(db.Integer, nullable=False, default=0)
    first_solve_at = db.Column(UTCDateTime, nullable=True)
    median_solve_seconds = db.Column(db.Integer, nullable=True)  # since event start
    updated_at = db.Column(UTCDateTime, nullable=False, default=utcnow)

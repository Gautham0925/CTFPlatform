"""Jeopardy engine models (Phase 4).

Challenge *content* (name, description, category, flags, hints, files, tags)
stays in native CTFd tables and is edited with native-compatible code. Arena
owns the event assignment, scoring configuration and all participant state.
"""

from CTFd.models import db
from CTFd.plugins.arena.models.activity import BigInt
from CTFd.plugins.arena.models.core import UTCDateTime, _in, utcnow

DIFFICULTIES = ("easy", "medium", "hard", "insane")
SCORING_MODES = ("static", "dynamic")
DECAY_FUNCTIONS = ("linear", "logarithmic")
SUBMISSION_STATUSES = ("correct", "incorrect", "ratelimited")
# Suggested categories (free text is allowed; these drive UI filters).
CATEGORIES = (
    "Web",
    "Cryptography",
    "Forensics",
    "Reverse Engineering",
    "Binary Exploitation",
    "OSINT",
    "Miscellaneous",
)


class ArenaEventChallenge(db.Model):
    __tablename__ = "arena_event_challenges"
    __table_args__ = (
        # A native challenge belongs to exactly one event (reuse = clone).
        db.UniqueConstraint("challenge_id", name="uq_arena_ec_challenge"),
        db.CheckConstraint(
            "difficulty IS NULL OR " + _in("difficulty", DIFFICULTIES),
            name="ck_arena_ec_difficulty",
        ),
        db.CheckConstraint(_in("scoring", SCORING_MODES), name="ck_arena_ec_scoring"),
        db.CheckConstraint(
            _in("decay_function", DECAY_FUNCTIONS), name="ck_arena_ec_decay_fn"
        ),
        db.CheckConstraint("points >= 0", name="ck_arena_ec_points"),
        db.Index("ix_arena_ec_event_visible_pos", "event_id", "visible", "position"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_events.id", ondelete="CASCADE", name="fk_arena_ec_event"),
        nullable=False,
    )
    challenge_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "challenges.id", ondelete="CASCADE", name="fk_arena_ec_challenge"
        ),
        nullable=False,
    )
    visible = db.Column(db.Boolean, nullable=False, default=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    difficulty = db.Column(db.String(16), nullable=True)
    position = db.Column(db.Integer, nullable=False, default=0)

    # Scoring
    scoring = db.Column(db.String(16), nullable=False, default="static")
    points = db.Column(db.Integer, nullable=False, default=100)  # static / initial
    minimum_points = db.Column(db.Integer, nullable=True)
    decay = db.Column(db.Integer, nullable=True)
    decay_function = db.Column(db.String(16), nullable=False, default="logarithmic")
    current_value = db.Column(db.Integer, nullable=False, default=100)
    solve_count = db.Column(db.Integer, nullable=False, default=0)

    # Availability and rules
    available_from = db.Column(UTCDateTime, nullable=True)
    available_until = db.Column(UTCDateTime, nullable=True)
    max_attempts = db.Column(db.Integer, nullable=True)  # NULL -> event default
    prerequisites = db.Column(db.JSON, nullable=True)  # [event_challenge_id, ...]
    per_entity_flag = db.Column(db.Boolean, nullable=False, default=False)
    # Docker-based challenge (Phase 8): instances are started from this image.
    image_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_images.id", ondelete="SET NULL", name="fk_arena_ec_image"),
        nullable=True,
    )

    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)
    updated_at = db.Column(UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow)


class ArenaSubmission(db.Model):
    """Every attempt (kept separate from accepted solves)."""

    __tablename__ = "arena_submissions"
    __table_args__ = (
        db.CheckConstraint(
            _in("status", SUBMISSION_STATUSES), name="ck_arena_sub_status"
        ),
        db.Index(
            "ix_arena_sub_entity_chal_time",
            "event_id",
            "entity_key",
            "event_challenge_id",
            "created_at",
        ),
        db.Index("ix_arena_sub_event_time", "event_id", "created_at"),
        db.Index("ix_arena_sub_event_status", "event_id", "status"),
    )

    id = db.Column(BigInt, primary_key=True, autoincrement=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_events.id", ondelete="CASCADE", name="fk_arena_sub_event"),
        nullable=False,
    )
    event_challenge_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_event_challenges.id", ondelete="CASCADE", name="fk_arena_sub_ec"
        ),
        nullable=False,
    )
    entity_key = db.Column(db.String(32), nullable=False)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_sub_user"),
        nullable=True,
    )
    status = db.Column(db.String(16), nullable=False)
    # Flags are never stored in clear: SHA-256 for duplicate analysis, and a
    # short preview of INCORRECT answers only (admin-visible).
    provided_hash = db.Column(db.String(64), nullable=False)
    provided_preview = db.Column(db.String(64), nullable=True)
    ip = db.Column(db.String(46), nullable=True)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class ArenaSolve(db.Model):
    """Accepted solve. UNIQUE(event, challenge, entity) = no double scoring."""

    __tablename__ = "arena_solves"
    __table_args__ = (
        db.UniqueConstraint(
            "event_id", "event_challenge_id", "entity_key", name="uq_arena_solve_once"
        ),
        db.Index("ix_arena_solve_event_time", "event_id", "created_at"),
        db.Index("ix_arena_solve_event_entity", "event_id", "entity_key"),
        db.Index("ix_arena_solve_event_user", "event_id", "user_id"),
    )

    id = db.Column(BigInt, primary_key=True, autoincrement=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_solve_event"
        ),
        nullable=False,
    )
    event_challenge_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_event_challenges.id", ondelete="CASCADE", name="fk_arena_solve_ec"
        ),
        nullable=False,
    )
    entity_key = db.Column(db.String(32), nullable=False)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_solve_user"),
        nullable=True,
    )
    submission_id = db.Column(
        BigInt,
        db.ForeignKey(
            "arena_submissions.id", ondelete="SET NULL", name="fk_arena_solve_sub"
        ),
        nullable=True,
    )
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class ArenaHintUnlock(db.Model):
    __tablename__ = "arena_hint_unlocks"
    __table_args__ = (
        db.UniqueConstraint(
            "event_id", "hint_id", "entity_key", name="uq_arena_hint_once"
        ),
        db.Index("ix_arena_hint_event_entity", "event_id", "entity_key"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_hint_event"
        ),
        nullable=False,
    )
    event_challenge_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_event_challenges.id", ondelete="CASCADE", name="fk_arena_hint_ec"
        ),
        nullable=False,
    )
    hint_id = db.Column(
        db.Integer,
        db.ForeignKey("hints.id", ondelete="CASCADE", name="fk_arena_hint_hint"),
        nullable=False,
    )
    entity_key = db.Column(db.String(32), nullable=False)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_hint_user"),
        nullable=True,
    )
    cost = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)

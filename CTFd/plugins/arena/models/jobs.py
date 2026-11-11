"""Background job queue, worker registry and leader lease (Phase 6)."""

from CTFd.models import db
from CTFd.plugins.arena.models.activity import BigInt
from CTFd.plugins.arena.models.core import UTCDateTime, _in, utcnow

JOB_STATUSES = ("queued", "running", "done", "dead")


class ArenaJob(db.Model):
    __tablename__ = "arena_jobs"
    __table_args__ = (
        # Set while queued/running, NULL when finished: duplicate protection
        # for active jobs without blocking future runs of the same key.
        db.UniqueConstraint("dedupe_key", name="uq_arena_jobs_dedupe"),
        db.CheckConstraint(_in("status", JOB_STATUSES), name="ck_arena_jobs_status"),
        db.Index("ix_arena_jobs_claim", "status", "run_after", "priority"),
        db.Index("ix_arena_jobs_kind_status", "kind", "status"),
        db.Index("ix_arena_jobs_finished", "status", "finished_at"),
    )

    id = db.Column(BigInt, primary_key=True, autoincrement=True)
    kind = db.Column(db.String(48), nullable=False)
    payload = db.Column(db.JSON, nullable=True)
    status = db.Column(db.String(16), nullable=False, default="queued")
    priority = db.Column(db.Integer, nullable=False, default=100)  # lower first
    attempts = db.Column(db.Integer, nullable=False, default=0)
    max_attempts = db.Column(db.Integer, nullable=False, default=5)
    run_after = db.Column(UTCDateTime, nullable=False, default=utcnow)
    dedupe_key = db.Column(db.String(128), nullable=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="SET NULL", name="fk_arena_jobs_event"
        ),
        nullable=True,
    )
    locked_by = db.Column(db.String(96), nullable=True)
    lease_until = db.Column(UTCDateTime, nullable=True)
    last_error = db.Column(db.String(2000), nullable=True)
    result = db.Column(db.JSON, nullable=True)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)
    started_at = db.Column(UTCDateTime, nullable=True)
    finished_at = db.Column(UTCDateTime, nullable=True)


class ArenaWorker(db.Model):
    __tablename__ = "arena_workers"

    id = db.Column(db.String(96), primary_key=True)  # host:pid:random
    hostname = db.Column(db.String(64), nullable=False)
    pid = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(16), nullable=False, default="running")
    started_at = db.Column(UTCDateTime, nullable=False, default=utcnow)
    heartbeat_at = db.Column(UTCDateTime, nullable=False, default=utcnow)
    jobs_done = db.Column(db.Integer, nullable=False, default=0)
    jobs_failed = db.Column(db.Integer, nullable=False, default=0)
    current_job_id = db.Column(BigInt, nullable=True)
    version = db.Column(db.String(32), nullable=True)


class ArenaLease(db.Model):
    """Named DB lease (leader election). Works even when Redis is down."""

    __tablename__ = "arena_leases"

    name = db.Column(db.String(64), primary_key=True)
    holder = db.Column(db.String(96), nullable=True)
    expires_at = db.Column(UTCDateTime, nullable=False, default=utcnow)

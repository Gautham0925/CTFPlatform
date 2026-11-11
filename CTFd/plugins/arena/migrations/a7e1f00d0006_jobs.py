"""Phase 6: background jobs, workers and leases

Revision ID: a7e1f00d0006
Revises: a7e1f00d0005

Generated with Alembic autogenerate against MariaDB at the previous head,
then reviewed. Frozen: does not import Arena models.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql  # noqa: F401

revision = "a7e1f00d0006"
down_revision = "a7e1f00d0005"
branch_labels = None
depends_on = None


def upgrade(op=None):
    op.create_table(
        "arena_leases",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("holder", sa.String(length=96), nullable=True),
        sa.Column(
            "expires_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "arena_workers",
        sa.Column("id", sa.String(length=96), nullable=False),
        sa.Column("hostname", sa.String(length=64), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.Column(
            "heartbeat_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.Column("jobs_done", sa.Integer(), nullable=False),
        sa.Column("jobs_failed", sa.Integer(), nullable=False),
        sa.Column(
            "current_job_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column("version", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "arena_jobs",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "run_after",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.Column("dedupe_key", sa.String(length=128), nullable=True),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("locked_by", sa.String(length=96), nullable=True),
        sa.Column(
            "lease_until",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column("last_error", sa.String(length=2000), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column(
            "finished_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'dead')",
            name="ck_arena_jobs_status",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_jobs_event",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_arena_jobs_dedupe"),
    )
    op.create_index(
        "ix_arena_jobs_claim",
        "arena_jobs",
        ["status", "run_after", "priority"],
        unique=False,
    )
    op.create_index(
        "ix_arena_jobs_finished", "arena_jobs", ["status", "finished_at"], unique=False
    )
    op.create_index(
        "ix_arena_jobs_kind_status", "arena_jobs", ["kind", "status"], unique=False
    )


def downgrade(op=None):
    op.drop_table("arena_jobs")
    op.drop_table("arena_workers")
    op.drop_table("arena_leases")

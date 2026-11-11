"""Phase 7: analytics rollup tables

Revision ID: a7e1f00d0007
Revises: a7e1f00d0006

Generated with Alembic autogenerate against MariaDB at the previous head,
then reviewed. Frozen: does not import Arena models.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql  # noqa: F401

revision = "a7e1f00d0007"
down_revision = "a7e1f00d0006"
branch_labels = None
depends_on = None


def upgrade(op=None):
    op.create_table(
        "arena_stats_hourly",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column(
            "bucket_start",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.Column("active_users", sa.Integer(), nullable=False),
        sa.Column("active_entities", sa.Integer(), nullable=False),
        sa.Column("submissions", sa.Integer(), nullable=False),
        sa.Column("correct", sa.Integer(), nullable=False),
        sa.Column("registrations", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_sth_event",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", "bucket_start"),
    )
    op.create_table(
        "arena_stats_challenge",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("event_challenge_id", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("incorrect", sa.Integer(), nullable=False),
        sa.Column("solves", sa.Integer(), nullable=False),
        sa.Column("unique_attempters", sa.Integer(), nullable=False),
        sa.Column(
            "first_solve_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column("median_solve_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_challenge_id"],
            ["arena_event_challenges.id"],
            name="fk_arena_stc_ec",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_stc_event",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", "event_challenge_id"),
    )


def downgrade(op=None):
    op.drop_table("arena_stats_challenge")
    op.drop_table("arena_stats_hourly")

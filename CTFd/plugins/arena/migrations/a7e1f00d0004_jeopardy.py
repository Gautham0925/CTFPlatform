"""Phase 4: event challenges, submissions, solves, hint unlocks

Revision ID: a7e1f00d0004
Revises: a7e1f00d0003

Generated with Alembic autogenerate against MariaDB at the previous head,
then reviewed. Frozen: does not import Arena models.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql  # noqa: F401

revision = "a7e1f00d0004"
down_revision = "a7e1f00d0003"
branch_labels = None
depends_on = None


def upgrade(op=None):
    op.create_table(
        "arena_event_challenges",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("challenge_id", sa.Integer(), nullable=False),
        sa.Column("visible", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("difficulty", sa.String(length=16), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("scoring", sa.String(length=16), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("minimum_points", sa.Integer(), nullable=True),
        sa.Column("decay", sa.Integer(), nullable=True),
        sa.Column("decay_function", sa.String(length=16), nullable=False),
        sa.Column("current_value", sa.Integer(), nullable=False),
        sa.Column("solve_count", sa.Integer(), nullable=False),
        sa.Column(
            "available_from",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column(
            "available_until",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column("max_attempts", sa.Integer(), nullable=True),
        sa.Column("prerequisites", sa.JSON(), nullable=True),
        sa.Column("per_entity_flag", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decay_function IN ('linear', 'logarithmic')", name="ck_arena_ec_decay_fn"
        ),
        sa.CheckConstraint(
            "difficulty IS NULL OR difficulty IN ('easy', 'medium', 'hard', 'insane')",
            name="ck_arena_ec_difficulty",
        ),
        sa.CheckConstraint(
            "scoring IN ('static', 'dynamic')", name="ck_arena_ec_scoring"
        ),
        sa.CheckConstraint("points >= 0", name="ck_arena_ec_points"),
        sa.ForeignKeyConstraint(
            ["challenge_id"],
            ["challenges.id"],
            name="fk_arena_ec_challenge",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_ec_event",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("challenge_id", name="uq_arena_ec_challenge"),
    )
    op.create_index(
        "ix_arena_ec_event_visible_pos",
        "arena_event_challenges",
        ["event_id", "visible", "position"],
        unique=False,
    )
    op.create_table(
        "arena_hint_unlocks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("event_challenge_id", sa.Integer(), nullable=False),
        sa.Column("hint_id", sa.Integer(), nullable=False),
        sa.Column("entity_key", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("cost", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_challenge_id"],
            ["arena_event_challenges.id"],
            name="fk_arena_hint_ec",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_hint_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["hint_id"], ["hints.id"], name="fk_arena_hint_hint", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_arena_hint_user", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id", "hint_id", "entity_key", name="uq_arena_hint_once"
        ),
    )
    op.create_index(
        "ix_arena_hint_event_entity",
        "arena_hint_unlocks",
        ["event_id", "entity_key"],
        unique=False,
    )
    op.create_table(
        "arena_submissions",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("event_challenge_id", sa.Integer(), nullable=False),
        sa.Column("entity_key", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provided_hash", sa.String(length=64), nullable=False),
        sa.Column("provided_preview", sa.String(length=64), nullable=True),
        sa.Column("ip", sa.String(length=46), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('correct', 'incorrect', 'ratelimited')",
            name="ck_arena_sub_status",
        ),
        sa.ForeignKeyConstraint(
            ["event_challenge_id"],
            ["arena_event_challenges.id"],
            name="fk_arena_sub_ec",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_sub_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_arena_sub_user", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_arena_sub_entity_chal_time",
        "arena_submissions",
        ["event_id", "entity_key", "event_challenge_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_arena_sub_event_status",
        "arena_submissions",
        ["event_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_arena_sub_event_time",
        "arena_submissions",
        ["event_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "arena_solves",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("event_challenge_id", sa.Integer(), nullable=False),
        sa.Column("entity_key", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column(
            "submission_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_challenge_id"],
            ["arena_event_challenges.id"],
            name="fk_arena_solve_ec",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_solve_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["arena_submissions.id"],
            name="fk_arena_solve_sub",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_arena_solve_user", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id", "event_challenge_id", "entity_key", name="uq_arena_solve_once"
        ),
    )
    op.create_index(
        "ix_arena_solve_event_entity",
        "arena_solves",
        ["event_id", "entity_key"],
        unique=False,
    )
    op.create_index(
        "ix_arena_solve_event_time",
        "arena_solves",
        ["event_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_arena_solve_event_user",
        "arena_solves",
        ["event_id", "user_id"],
        unique=False,
    )


def downgrade(op=None):
    op.drop_table("arena_solves")
    op.drop_table("arena_submissions")
    op.drop_table("arena_hint_unlocks")
    op.drop_table("arena_event_challenges")

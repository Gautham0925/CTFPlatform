"""Phase 5: scoring ledger, scoreboard snapshots, versioned scoring configs

Revision ID: a7e1f00d0005
Revises: a7e1f00d0004

Generated with Alembic autogenerate against MariaDB at the previous head,
then reviewed. Frozen: does not import Arena models.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql  # noqa: F401

revision = "a7e1f00d0005"
down_revision = "a7e1f00d0004"
branch_labels = None
depends_on = None


def upgrade(op=None):
    op.create_table(
        "arena_scoreboard_snapshots",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_sbsnap_event",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", "scope"),
    )
    op.create_table(
        "arena_scoring_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_arena_scfg_creator",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_scfg_event",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "version", name="uq_arena_scfg_event_version"),
    )
    op.create_table(
        "arena_score_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("entity_key", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("component", sa.String(length=16), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=96), nullable=False),
        sa.Column("event_challenge_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "component IN ('solve', 'first_blood', 'hint', 'bonus', 'penalty', 'ad_attack', 'ad_defense', 'ad_sla')",
            name="ck_arena_score_component",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_arena_score_actor",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["event_challenge_id"],
            ["arena_event_challenges.id"],
            name="fk_arena_score_ec",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_score_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_arena_score_user", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id", "source", "source_ref", name="uq_arena_score_source"
        ),
    )
    op.create_index(
        "ix_arena_score_event_entity",
        "arena_score_events",
        ["event_id", "entity_key"],
        unique=False,
    )
    op.create_index(
        "ix_arena_score_event_time",
        "arena_score_events",
        ["event_id", "created_at"],
        unique=False,
    )


def downgrade(op=None):
    op.drop_table("arena_score_events")
    op.drop_table("arena_scoring_configs")
    op.drop_table("arena_scoreboard_snapshots")

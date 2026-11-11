"""Phase 10: historical score snapshots

Revision ID: a7e1f00d0010
Revises: a7e1f00d0009

Generated with Alembic autogenerate against MariaDB at the previous head,
then reviewed. Frozen: does not import Arena models.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql  # noqa: F401

revision = "a7e1f00d0010"
down_revision = "a7e1f00d0009"
branch_labels = None
depends_on = None


def upgrade(op=None):
    op.create_table(
        "arena_score_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("ref", sa.String(length=64), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_ssnap_event",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_arena_ssnap_event_time",
        "arena_score_snapshots",
        ["event_id", "created_at"],
        unique=False,
    )


def downgrade(op=None):
    op.drop_table("arena_score_snapshots")

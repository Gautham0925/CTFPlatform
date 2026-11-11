"""Phase 3: event teams, members and team invites

Revision ID: a7e1f00d0003
Revises: a7e1f00d0002

Generated with Alembic autogenerate against MariaDB at the previous head,
then reviewed. Frozen: does not import Arena models.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql  # noqa: F401

revision = "a7e1f00d0003"
down_revision = "a7e1f00d0002"
branch_labels = None
depends_on = None


def upgrade(op=None):
    op.create_table(
        "arena_teams",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("captain_user_id", sa.Integer(), nullable=True),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("join_code_hash", sa.String(length=64), nullable=True),
        sa.Column("affiliation", sa.String(length=128), nullable=True),
        sa.Column("country", sa.String(length=32), nullable=True),
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
            "status IN ('active', 'banned', 'disbanded')", name="ck_arena_teams_status"
        ),
        sa.CheckConstraint(
            "visibility IN ('public', 'private')", name="ck_arena_teams_visibility"
        ),
        sa.CheckConstraint("member_count >= 0", name="ck_arena_teams_member_count"),
        sa.ForeignKeyConstraint(
            ["captain_user_id"],
            ["users.id"],
            name="fk_arena_teams_captain",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_teams_event",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "name", name="uq_arena_teams_event_name"),
        sa.UniqueConstraint("join_code_hash", name="uq_arena_teams_join_code"),
    )
    op.create_index(
        "ix_arena_teams_event_status",
        "arena_teams",
        ["event_id", "status"],
        unique=False,
    )
    op.create_table(
        "arena_team_invites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("invited_by", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("pending_key", sa.String(length=32), nullable=True),
        sa.Column(
            "expires_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.Column(
            "responded_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'cancelled', 'expired')",
            name="ck_arena_tinv_status",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_tinv_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"],
            ["users.id"],
            name="fk_arena_tinv_inviter",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["arena_teams.id"],
            name="fk_arena_tinv_team",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_arena_tinv_user", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pending_key", name="uq_arena_tinv_pending"),
    )
    op.create_index(
        "ix_arena_tinv_team", "arena_team_invites", ["team_id"], unique=False
    )
    op.create_index(
        "ix_arena_tinv_user_status",
        "arena_team_invites",
        ["user_id", "status"],
        unique=False,
    )
    op.create_table(
        "arena_team_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('captain', 'member')", name="ck_arena_members_role"
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_members_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["arena_teams.id"],
            name="fk_arena_members_team",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_arena_members_user", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "user_id", name="uq_arena_members_event_user"),
    )
    op.create_index(
        "ix_arena_members_team", "arena_team_members", ["team_id"], unique=False
    )


def downgrade(op=None):
    op.drop_table("arena_team_members")
    op.drop_table("arena_team_invites")
    op.drop_table("arena_teams")

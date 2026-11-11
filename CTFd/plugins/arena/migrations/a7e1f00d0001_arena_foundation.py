"""Arena foundation: events, registrations, roles

Revision ID: a7e1f00d0001
Revises:
Create Date: 2026-11-11 00:00:00

Creates Arena-owned tables only. No native CTFd table is modified, so
downgrade() leaves native data untouched.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from CTFd.plugins.migrations import get_all_tables

revision = "a7e1f00d0001"
down_revision = None
branch_labels = None
depends_on = None

DT = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def _in(column, values):
    return "{} IN ({})".format(column, ", ".join("'%s'" % v for v in values))


def upgrade(op=None):
    tables = set(get_all_tables(op))

    if "arena_events" not in tables:
        op.create_table(
            "arena_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("slug", sa.String(64), nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("rules", sa.Text(), nullable=True),
            sa.Column("type", sa.String(16), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("visibility", sa.String(16), nullable=False),
            sa.Column("starts_at", DT, nullable=True),
            sa.Column("ends_at", DT, nullable=True),
            sa.Column("freeze_at", DT, nullable=True),
            sa.Column("registration_open", sa.Boolean(), nullable=False),
            sa.Column("registration_opens_at", DT, nullable=True),
            sa.Column("registration_closes_at", DT, nullable=True),
            sa.Column("requires_approval", sa.Boolean(), nullable=False),
            sa.Column("max_participants", sa.Integer(), nullable=True),
            sa.Column("team_mode", sa.String(16), nullable=False),
            sa.Column("team_size_min", sa.Integer(), nullable=False),
            sa.Column("team_size_max", sa.Integer(), nullable=False),
            sa.Column("jeopardy_enabled", sa.Boolean(), nullable=False),
            sa.Column("ad_enabled", sa.Boolean(), nullable=False),
            sa.Column("sealed_module_id", sa.Integer(), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_at", DT, nullable=False),
            sa.Column("updated_at", DT, nullable=False),
            sa.UniqueConstraint("slug", name="uq_arena_events_slug"),
            sa.UniqueConstraint(
                "sealed_module_id", name="uq_arena_events_sealed_module"
            ),
            sa.ForeignKeyConstraint(
                ["sealed_module_id"],
                ["modules.id"],
                ondelete="RESTRICT",
                name="fk_arena_events_sealed_module",
            ),
            sa.ForeignKeyConstraint(
                ["created_by"],
                ["users.id"],
                ondelete="SET NULL",
                name="fk_arena_events_creator",
            ),
            sa.CheckConstraint(
                _in("type", ("jeopardy", "ad", "mixed")), name="ck_arena_events_type"
            ),
            sa.CheckConstraint(
                _in(
                    "status",
                    ("draft", "published", "running", "paused", "ended", "archived"),
                ),
                name="ck_arena_events_status",
            ),
            sa.CheckConstraint(
                _in("visibility", ("public", "unlisted", "private")),
                name="ck_arena_events_visibility",
            ),
            sa.CheckConstraint(
                _in("team_mode", ("individual", "team")),
                name="ck_arena_events_team_mode",
            ),
            mysql_engine="InnoDB",
        )
        op.create_index(
            "ix_arena_events_status_starts", "arena_events", ["status", "starts_at"]
        )

    if "arena_registrations" not in tables:
        op.create_table(
            "arena_registrations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("event_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("approved_by", sa.Integer(), nullable=True),
            sa.Column("created_at", DT, nullable=False),
            sa.Column("updated_at", DT, nullable=False),
            sa.UniqueConstraint("event_id", "user_id", name="uq_arena_reg_event_user"),
            sa.ForeignKeyConstraint(
                ["event_id"],
                ["arena_events.id"],
                ondelete="CASCADE",
                name="fk_arena_reg_event",
            ),
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"], ondelete="CASCADE", name="fk_arena_reg_user"
            ),
            sa.ForeignKeyConstraint(
                ["approved_by"],
                ["users.id"],
                ondelete="SET NULL",
                name="fk_arena_reg_approver",
            ),
            sa.CheckConstraint(
                _in(
                    "status",
                    ("pending", "approved", "rejected", "withdrawn", "banned"),
                ),
                name="ck_arena_reg_status",
            ),
            sa.CheckConstraint(
                _in("source", ("self", "admin", "import")), name="ck_arena_reg_source"
            ),
            mysql_engine="InnoDB",
        )
        op.create_index(
            "ix_arena_reg_event_status", "arena_registrations", ["event_id", "status"]
        )
        op.create_index("ix_arena_reg_user", "arena_registrations", ["user_id"])

    if "arena_roles" not in tables:
        op.create_table(
            "arena_roles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("event_id", sa.Integer(), nullable=True),
            sa.Column("role", sa.String(32), nullable=False),
            sa.Column("granted_by", sa.Integer(), nullable=True),
            sa.Column("created_at", DT, nullable=False),
            sa.UniqueConstraint(
                "user_id", "event_id", "role", name="uq_arena_roles_user_event_role"
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["users.id"],
                ondelete="CASCADE",
                name="fk_arena_roles_user",
            ),
            sa.ForeignKeyConstraint(
                ["event_id"],
                ["arena_events.id"],
                ondelete="CASCADE",
                name="fk_arena_roles_event",
            ),
            sa.ForeignKeyConstraint(
                ["granted_by"],
                ["users.id"],
                ondelete="SET NULL",
                name="fk_arena_roles_granter",
            ),
            sa.CheckConstraint(
                _in("role", ("event_admin", "ad_operator", "cert_manager", "auditor")),
                name="ck_arena_roles_role",
            ),
            mysql_engine="InnoDB",
        )
        op.create_index("ix_arena_roles_event", "arena_roles", ["event_id"])


def downgrade(op=None):
    tables = set(get_all_tables(op))
    # Reverse dependency order; only Arena tables are touched.
    for table in ("arena_roles", "arena_registrations", "arena_events"):
        if table in tables:
            op.drop_table(table)

"""Phase 2: event lifecycle statuses, roles, branding/settings, capacity, invites

Revision ID: a7e1f00d0002
Revises: a7e1f00d0001
Create Date: 2026-11-11 12:00:00

Status values: published->upcoming, running->active, ended->completed.
Role values:   event_admin->organizer, auditor->analyst
               (+ challenge_manager, scoreboard_operator).
Adds arena_events.branding/settings/seat_count, arena_event_invites,
arena_registrations.invite_id.

Arena migrations target MySQL/MariaDB. On SQLite (development/tests only)
the schema is produced by create_all(); this revision then only manages the
new table so that downgrade-to-base still works.

Downgrade maps values back; roles that did not exist before
(challenge_manager, scoreboard_operator) are deleted.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from CTFd.plugins.migrations import get_all_tables, get_columns_for_table

revision = "a7e1f00d0002"
down_revision = "a7e1f00d0001"
branch_labels = None
depends_on = None

DT = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")

OLD_STATUSES = ("draft", "published", "running", "paused", "ended", "archived")
NEW_STATUSES = ("draft", "upcoming", "active", "paused", "completed", "archived")
STATUS_MAP = {"published": "upcoming", "running": "active", "ended": "completed"}

OLD_ROLES = ("event_admin", "ad_operator", "cert_manager", "auditor")
NEW_ROLES = (
    "organizer",
    "challenge_manager",
    "scoreboard_operator",
    "ad_operator",
    "cert_manager",
    "analyst",
)
ROLE_MAP = {"event_admin": "organizer", "auditor": "analyst"}


def _in(column, values):
    return "{} IN ({})".format(column, ", ".join("'%s'" % v for v in values))


def _is_sqlite(op):
    return op.get_bind().dialect.name == "sqlite"


def _swap_check(op, table, name, column, values, mapping):
    op.drop_constraint(name, table, type_="check")
    for old, new in mapping.items():
        op.execute(
            # Identifiers are module constants; values are bound parameters.
            sa.text(
                f"UPDATE {table} SET {column} = :new WHERE {column} = :old"  # noqa: S608
            ).bindparams(new=new, old=old)
        )
    op.create_check_constraint(name, table, _in(column, values))


def upgrade(op=None):
    tables = set(get_all_tables(op))
    if "arena_event_invites" not in tables:
        op.create_table(
            "arena_event_invites",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("event_id", sa.Integer(), nullable=False),
            sa.Column("code_hash", sa.String(64), nullable=False),
            sa.Column("label", sa.String(128), nullable=True),
            sa.Column("max_uses", sa.Integer(), nullable=True),
            sa.Column("uses", sa.Integer(), nullable=False),
            sa.Column("expires_at", DT, nullable=True),
            sa.Column("revoked_at", DT, nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_at", DT, nullable=False),
            sa.UniqueConstraint("code_hash", name="uq_arena_invites_code"),
            sa.ForeignKeyConstraint(
                ["event_id"],
                ["arena_events.id"],
                ondelete="CASCADE",
                name="fk_arena_invites_event",
            ),
            sa.ForeignKeyConstraint(
                ["created_by"],
                ["users.id"],
                ondelete="SET NULL",
                name="fk_arena_invites_creator",
            ),
            mysql_engine="InnoDB",
        )
        op.create_index("ix_arena_invites_event", "arena_event_invites", ["event_id"])

    if "arena_activity" not in tables:
        big = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
        op.create_table(
            "arena_activity",
            sa.Column("id", big, primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.Integer(), nullable=True),
            sa.Column("actor_user_id", sa.Integer(), nullable=True),
            sa.Column("actor_type", sa.String(16), nullable=False),
            sa.Column("action", sa.String(48), nullable=False),
            sa.Column("target_type", sa.String(32), nullable=True),
            sa.Column("target_id", sa.String(64), nullable=True),
            sa.Column("meta", sa.JSON(), nullable=True),
            sa.Column("created_at", DT, nullable=False),
            sa.ForeignKeyConstraint(
                ["event_id"],
                ["arena_events.id"],
                ondelete="SET NULL",
                name="fk_arena_activity_event",
            ),
            sa.ForeignKeyConstraint(
                ["actor_user_id"],
                ["users.id"],
                ondelete="SET NULL",
                name="fk_arena_activity_actor",
            ),
            mysql_engine="InnoDB",
        )
        op.create_index(
            "ix_arena_activity_event_time", "arena_activity", ["event_id", "created_at"]
        )
        op.create_index(
            "ix_arena_activity_actor_time",
            "arena_activity",
            ["actor_user_id", "created_at"],
        )
        op.create_index(
            "ix_arena_activity_action_time", "arena_activity", ["action", "created_at"]
        )

    if _is_sqlite(op):
        return

    ev_cols = set(get_columns_for_table(op, "arena_events", names_only=True))
    if "branding" not in ev_cols:
        op.add_column("arena_events", sa.Column("branding", sa.JSON(), nullable=True))
    if "settings" not in ev_cols:
        op.add_column("arena_events", sa.Column("settings", sa.JSON(), nullable=True))
    if "seat_count" not in ev_cols:
        op.add_column(
            "arena_events",
            sa.Column("seat_count", sa.Integer(), nullable=False, server_default="0"),
        )
        # Seats = existing pending/approved registrations.
        op.execute(
            "UPDATE arena_events e SET seat_count = (SELECT COUNT(*) FROM "
            "arena_registrations r WHERE r.event_id = e.id AND "
            "r.status IN ('pending', 'approved'))"
        )
        op.alter_column(
            "arena_events",
            "seat_count",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=None,
        )

    reg_cols = set(get_columns_for_table(op, "arena_registrations", names_only=True))
    if "invite_id" not in reg_cols:
        op.add_column(
            "arena_registrations", sa.Column("invite_id", sa.Integer(), nullable=True)
        )
        op.create_foreign_key(
            "fk_arena_reg_invite",
            "arena_registrations",
            "arena_event_invites",
            ["invite_id"],
            ["id"],
            ondelete="SET NULL",
        )

    _swap_check(
        op, "arena_events", "ck_arena_events_status", "status", NEW_STATUSES, STATUS_MAP
    )
    op.execute(
        "DELETE FROM arena_roles WHERE role NOT IN ({})".format(  # noqa: S608 (constants)
            ", ".join("'%s'" % r for r in OLD_ROLES)
        )
    )
    _swap_check(op, "arena_roles", "ck_arena_roles_role", "role", NEW_ROLES, ROLE_MAP)


def downgrade(op=None):
    tables = set(get_all_tables(op))
    if not _is_sqlite(op):
        _swap_check(
            op,
            "arena_events",
            "ck_arena_events_status",
            "status",
            OLD_STATUSES,
            {v: k for k, v in STATUS_MAP.items()},
        )
        op.execute(
            "DELETE FROM arena_roles WHERE role IN "
            "('challenge_manager', 'scoreboard_operator')"
        )
        _swap_check(
            op,
            "arena_roles",
            "ck_arena_roles_role",
            "role",
            OLD_ROLES,
            {v: k for k, v in ROLE_MAP.items()},
        )
        reg_cols = set(
            get_columns_for_table(op, "arena_registrations", names_only=True)
        )
        if "invite_id" in reg_cols:
            op.drop_constraint(
                "fk_arena_reg_invite", "arena_registrations", type_="foreignkey"
            )
            op.drop_column("arena_registrations", "invite_id")
        ev_cols = set(get_columns_for_table(op, "arena_events", names_only=True))
        for col in ("seat_count", "settings", "branding"):
            if col in ev_cols:
                op.drop_column("arena_events", col)
    if "arena_activity" in tables:
        op.drop_table("arena_activity")
    if "arena_event_invites" in tables:
        if _is_sqlite(op):
            # registrations.invite_id references this table (create_all schema).
            op.execute("UPDATE arena_registrations SET invite_id = NULL")
        op.drop_table("arena_event_invites")

"""Phase 11: external lab providers, event labs, lab sessions

Revision ID: a7e1f00d0011
Revises: a7e1f00d0010

Generated with Alembic autogenerate against MariaDB at the previous head,
then reviewed. Frozen: does not import Arena models.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql  # noqa: F401

revision = "a7e1f00d0011"
down_revision = "a7e1f00d0010"
branch_labels = None
depends_on = None


def upgrade(op=None):
    op.create_table(
        "arena_lab_providers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("adapter", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.String(length=255), nullable=True),
        sa.Column("credentials_enc", sa.Text(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_health", sa.JSON(), nullable=True),
        sa.Column(
            "last_health_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "adapter IN ('mock', 'generic_http')", name="ck_arena_labp_adapter"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_arena_labp_name"),
    )
    op.create_table(
        "arena_event_labs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("ttl_minutes", sa.Integer(), nullable=False),
        sa.Column("max_sessions", sa.Integer(), nullable=False),
        sa.Column("completion_points", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_elab_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["arena_lab_providers.id"],
            name="fk_arena_elab_provider",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id", "provider_id", "template_id", name="uq_arena_elab_template"
        ),
    )
    op.create_table(
        "arena_lab_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uuid", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("event_lab_id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("entity_key", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("active_key", sa.String(length=96), nullable=True),
        sa.Column("connection_enc", sa.Text(), nullable=True),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.Column(
            "expires_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
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
            "status IN ('requested', 'provisioning', 'ready', 'stopping', 'released', 'expired', 'failed')",
            name="ck_arena_labs_status",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_labs_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_lab_id"],
            ["arena_event_labs.id"],
            name="fk_arena_labs_elab",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["arena_lab_providers.id"],
            name="fk_arena_labs_provider",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_arena_labs_user", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("active_key", name="uq_arena_labs_active"),
        sa.UniqueConstraint("uuid", name="uq_arena_labs_uuid"),
    )
    op.create_index(
        "ix_arena_labs_event_entity",
        "arena_lab_sessions",
        ["event_id", "entity_key"],
        unique=False,
    )
    op.create_index(
        "ix_arena_labs_status_exp",
        "arena_lab_sessions",
        ["status", "expires_at"],
        unique=False,
    )


def downgrade(op=None):
    op.drop_table("arena_lab_sessions")
    op.drop_table("arena_event_labs")
    op.drop_table("arena_lab_providers")

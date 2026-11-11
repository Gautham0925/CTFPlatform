"""Phase 8: challenge images, instances, event challenge image link

Revision ID: a7e1f00d0008
Revises: a7e1f00d0007

Generated with Alembic autogenerate against MariaDB at the previous head,
then reviewed. Frozen: does not import Arena models.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql  # noqa: F401

revision = "a7e1f00d0008"
down_revision = "a7e1f00d0007"
branch_labels = None
depends_on = None


def upgrade(op=None):
    op.create_table(
        "arena_images",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("reference", sa.String(length=255), nullable=False),
        sa.Column("ports", sa.JSON(), nullable=False),
        sa.Column("cpu_millis", sa.Integer(), nullable=False),
        sa.Column("memory_mb", sa.Integer(), nullable=False),
        sa.Column("pids", sa.Integer(), nullable=False),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "validated_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "version", name="uq_arena_images_name_version"),
        sa.UniqueConstraint("reference", name="uq_arena_images_reference"),
    )
    op.create_table(
        "arena_instances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uuid", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("event_challenge_id", sa.Integer(), nullable=True),
        sa.Column("image_id", sa.Integer(), nullable=False),
        sa.Column("entity_key", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("active_key", sa.String(length=96), nullable=True),
        sa.Column("host", sa.String(length=255), nullable=True),
        sa.Column("ports", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.Column(
            "expires_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column(
            "last_status_at",
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
            "status IN ('requested', 'starting', 'running', 'stopping', 'stopped', 'failed', 'expired')",
            name="ck_arena_inst_status",
        ),
        sa.ForeignKeyConstraint(
            ["event_challenge_id"],
            ["arena_event_challenges.id"],
            name="fk_arena_inst_ec",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_inst_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["image_id"],
            ["arena_images.id"],
            name="fk_arena_inst_image",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_arena_inst_user", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("active_key", name="uq_arena_inst_active"),
        sa.UniqueConstraint("uuid", name="uq_arena_inst_uuid"),
    )
    op.create_index(
        "ix_arena_inst_event_entity",
        "arena_instances",
        ["event_id", "entity_key", "status"],
        unique=False,
    )
    op.create_index(
        "ix_arena_inst_status_exp",
        "arena_instances",
        ["status", "expires_at"],
        unique=False,
    )
    if op.get_bind().dialect.name == "sqlite":
        return  # SQLite (dev/tests): column comes from create_all()
    op.add_column(
        "arena_event_challenges", sa.Column("image_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_arena_ec_image",
        "arena_event_challenges",
        "arena_images",
        ["image_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade(op=None):
    if op.get_bind().dialect.name == "sqlite":
        # SQLite cannot drop FK columns without a table rebuild; the column is
        # harmless and arena_event_challenges is dropped at base anyway.
        op.execute("UPDATE arena_event_challenges SET image_id = NULL")
    else:
        op.drop_constraint(
            "fk_arena_ec_image", "arena_event_challenges", type_="foreignkey"
        )
        op.drop_column("arena_event_challenges", "image_id")
    op.drop_table("arena_instances")
    op.drop_table("arena_images")

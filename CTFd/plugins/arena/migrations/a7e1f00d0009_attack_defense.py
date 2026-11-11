"""Phase 9: Attack and Defense

Revision ID: a7e1f00d0009
Revises: a7e1f00d0008

Generated with Alembic autogenerate against MariaDB at the previous head,
then reviewed. Frozen: does not import Arena models.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql  # noqa: F401

revision = "a7e1f00d0009"
down_revision = "a7e1f00d0008"
branch_labels = None
depends_on = None


def upgrade(op=None):
    op.create_table(
        "arena_ad_rounds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "starts_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.Column(
            "ends_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.Column("checks_done", sa.Boolean(), nullable=False),
        sa.Column(
            "closed_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'closing', 'closed')", name="ck_arena_adr_status"
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_adr_event",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "number", name="uq_arena_adr_event_number"),
    )
    op.create_index(
        "ix_arena_adr_event_status",
        "arena_ad_rounds",
        ["event_id", "status"],
        unique=False,
    )
    op.create_table(
        "arena_ad_services",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("image_id", sa.Integer(), nullable=False),
        sa.Column("checker", sa.String(length=255), nullable=False),
        sa.Column("attack_points", sa.Integer(), nullable=False),
        sa.Column("defense_points", sa.Integer(), nullable=False),
        sa.Column("loss_points", sa.Integer(), nullable=False),
        sa.Column("sla_points", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_ads_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["image_id"],
            ["arena_images.id"],
            name="fk_arena_ads_image",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "name", name="uq_arena_ads_event_name"),
    )
    op.create_table(
        "arena_ad_checks",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("round_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('up', 'mumble', 'corrupt', 'down', 'error')",
            name="ck_arena_adk_status",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_adk_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["arena_ad_rounds.id"],
            name="fk_arena_adk_round",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["arena_ad_services.id"],
            name="fk_arena_adk_service",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["arena_teams.id"],
            name="fk_arena_adk_team",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "round_id", "team_id", "service_id", name="uq_arena_adk_round_team_svc"
        ),
    )
    op.create_index(
        "ix_arena_adk_event_team",
        "arena_ad_checks",
        ["event_id", "team_id"],
        unique=False,
    )
    op.create_table(
        "arena_ad_flags",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("round_id", sa.Integer(), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("flag_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_adf_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["arena_ad_rounds.id"],
            name="fk_arena_adf_round",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["arena_ad_services.id"],
            name="fk_arena_adf_service",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["arena_teams.id"],
            name="fk_arena_adf_team",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("flag_hash", name="uq_arena_adf_hash"),
        sa.UniqueConstraint(
            "round_id", "team_id", "service_id", name="uq_arena_adf_round_team_svc"
        ),
    )
    op.create_table(
        "arena_ad_captures",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column(
            "flag_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("attacker_team_id", sa.Integer(), nullable=False),
        sa.Column("victim_team_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("round_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["attacker_team_id"],
            ["arena_teams.id"],
            name="fk_arena_adc_attacker",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_adc_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["flag_id"],
            ["arena_ad_flags.id"],
            name="fk_arena_adc_flag",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["arena_ad_rounds.id"],
            name="fk_arena_adc_round",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_arena_adc_user", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "flag_id", "attacker_team_id", name="uq_arena_adc_flag_attacker"
        ),
    )
    op.create_index(
        "ix_arena_adc_event_round",
        "arena_ad_captures",
        ["event_id", "round_id"],
        unique=False,
    )
    op.create_table(
        "arena_ad_team_services",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("instance_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("mock_status", sa.String(length=16), nullable=True),
        sa.Column(
            "last_check_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_adts_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["arena_instances.id"],
            name="fk_arena_adts_instance",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["arena_ad_services.id"],
            name="fk_arena_adts_service",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["arena_teams.id"],
            name="fk_arena_adts_team",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("service_id", "team_id", name="uq_arena_adts_service_team"),
    )
    op.create_index(
        "ix_arena_adts_event", "arena_ad_team_services", ["event_id"], unique=False
    )


def downgrade(op=None):
    op.drop_table("arena_ad_team_services")
    op.drop_table("arena_ad_captures")
    op.drop_table("arena_ad_flags")
    op.drop_table("arena_ad_checks")
    op.drop_table("arena_ad_services")
    op.drop_table("arena_ad_rounds")

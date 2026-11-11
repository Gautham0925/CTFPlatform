"""Phase 12: optional certificates

Revision ID: a7e1f00d0012
Revises: a7e1f00d0011

Generated with Alembic autogenerate against MariaDB at the previous head,
then reviewed. Frozen: does not import Arena models.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql  # noqa: F401

revision = "a7e1f00d0012"
down_revision = "a7e1f00d0011"
branch_labels = None
depends_on = None


def upgrade(op=None):
    op.create_table(
        "arena_cert_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("body", sa.String(length=1000), nullable=False),
        sa.Column("criteria", sa.JSON(), nullable=True),
        sa.Column("accent_color", sa.String(length=7), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('participation', 'winner', 'top_n', 'team', 'custom')",
            name="ck_arena_ctpl_kind",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_ctpl_event",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "arena_certificates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=False),
        sa.Column("recipient_key", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("team_id", sa.Integer(), nullable=True),
        sa.Column("recipient_name", sa.String(length=128), nullable=False),
        sa.Column("team_name", sa.String(length=128), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("file_location", sa.String(length=255), nullable=True),
        sa.Column("verify_code", sa.String(length=64), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column("revoke_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'issued', 'failed', 'revoked')",
            name="ck_arena_cert_status",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["arena_events.id"],
            name="fk_arena_cert_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["arena_teams.id"],
            name="fk_arena_cert_team",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["arena_cert_templates.id"],
            name="fk_arena_cert_tpl",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_arena_cert_user", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_arena_cert_public"),
        sa.UniqueConstraint(
            "template_id", "recipient_key", name="uq_arena_cert_recipient"
        ),
    )
    op.create_index(
        "ix_arena_cert_event_user",
        "arena_certificates",
        ["event_id", "user_id"],
        unique=False,
    )


def downgrade(op=None):
    op.drop_table("arena_certificates")
    op.drop_table("arena_cert_templates")

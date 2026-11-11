"""Attack & Defense models (Phase 9). Independent of the Jeopardy tables."""

from CTFd.models import db
from CTFd.plugins.arena.models.activity import BigInt
from CTFd.plugins.arena.models.core import UTCDateTime, _in, utcnow

ROUND_STATUSES = ("active", "closing", "closed")
CHECK_STATUSES = ("up", "mumble", "corrupt", "down", "error")


class ArenaADService(db.Model):
    __tablename__ = "arena_ad_services"
    __table_args__ = (
        db.UniqueConstraint("event_id", "name", name="uq_arena_ads_event_name"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_events.id", ondelete="CASCADE", name="fk_arena_ads_event"),
        nullable=False,
    )
    name = db.Column(db.String(64), nullable=False)
    description = db.Column(db.Text, nullable=True)
    image_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_images.id", ondelete="RESTRICT", name="fk_arena_ads_image"
        ),
        nullable=False,
    )
    checker = db.Column(
        db.String(255), nullable=False, default="mock"
    )  # checker image ref | "mock"
    attack_points = db.Column(db.Integer, nullable=False, default=10)
    defense_points = db.Column(
        db.Integer, nullable=False, default=2
    )  # per uncompromised up round
    loss_points = db.Column(db.Integer, nullable=False, default=5)  # per stolen flag
    sla_points = db.Column(db.Integer, nullable=False, default=1)  # per 'up' check
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class ArenaADTeamService(db.Model):
    __tablename__ = "arena_ad_team_services"
    __table_args__ = (
        db.UniqueConstraint("service_id", "team_id", name="uq_arena_adts_service_team"),
        db.Index("ix_arena_adts_event", "event_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_events.id", ondelete="CASCADE", name="fk_arena_adts_event"
        ),
        nullable=False,
    )
    service_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_ad_services.id", ondelete="CASCADE", name="fk_arena_adts_service"
        ),
        nullable=False,
    )
    team_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_teams.id", ondelete="CASCADE", name="fk_arena_adts_team"),
        nullable=False,
    )
    instance_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_instances.id", ondelete="SET NULL", name="fk_arena_adts_instance"
        ),
        nullable=True,
    )
    status = db.Column(db.String(16), nullable=False, default="provisioning")
    mock_status = db.Column(
        db.String(16), nullable=True
    )  # MockRunner only (demos/tests)
    last_check_at = db.Column(UTCDateTime, nullable=True)


class ArenaADRound(db.Model):
    __tablename__ = "arena_ad_rounds"
    __table_args__ = (
        db.UniqueConstraint("event_id", "number", name="uq_arena_adr_event_number"),
        db.CheckConstraint(_in("status", ROUND_STATUSES), name="ck_arena_adr_status"),
        db.Index("ix_arena_adr_event_status", "event_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_events.id", ondelete="CASCADE", name="fk_arena_adr_event"),
        nullable=False,
    )
    number = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(16), nullable=False, default="active")
    starts_at = db.Column(UTCDateTime, nullable=False)
    ends_at = db.Column(UTCDateTime, nullable=False)
    checks_done = db.Column(db.Boolean, nullable=False, default=False)
    closed_at = db.Column(UTCDateTime, nullable=True)


class ArenaADFlag(db.Model):
    __tablename__ = "arena_ad_flags"
    __table_args__ = (
        db.UniqueConstraint("flag_hash", name="uq_arena_adf_hash"),
        db.UniqueConstraint(
            "round_id", "team_id", "service_id", name="uq_arena_adf_round_team_svc"
        ),
    )

    id = db.Column(BigInt, primary_key=True, autoincrement=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_events.id", ondelete="CASCADE", name="fk_arena_adf_event"),
        nullable=False,
    )
    round_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_ad_rounds.id", ondelete="CASCADE", name="fk_arena_adf_round"
        ),
        nullable=False,
    )
    round_number = db.Column(db.Integer, nullable=False)
    team_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_teams.id", ondelete="CASCADE", name="fk_arena_adf_team"),
        nullable=False,
    )
    service_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_ad_services.id", ondelete="CASCADE", name="fk_arena_adf_service"
        ),
        nullable=False,
    )
    flag_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class ArenaADCapture(db.Model):
    __tablename__ = "arena_ad_captures"
    __table_args__ = (
        # A team can capture a given flag only once.
        db.UniqueConstraint(
            "flag_id", "attacker_team_id", name="uq_arena_adc_flag_attacker"
        ),
        db.Index("ix_arena_adc_event_round", "event_id", "round_id"),
    )

    id = db.Column(BigInt, primary_key=True, autoincrement=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_events.id", ondelete="CASCADE", name="fk_arena_adc_event"),
        nullable=False,
    )
    flag_id = db.Column(
        BigInt,
        db.ForeignKey(
            "arena_ad_flags.id", ondelete="CASCADE", name="fk_arena_adc_flag"
        ),
        nullable=False,
    )
    attacker_team_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_teams.id", ondelete="CASCADE", name="fk_arena_adc_attacker"
        ),
        nullable=False,
    )
    victim_team_id = db.Column(db.Integer, nullable=False)
    service_id = db.Column(db.Integer, nullable=False)
    round_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_ad_rounds.id", ondelete="CASCADE", name="fk_arena_adc_round"
        ),
        nullable=False,
    )  # round in which the capture was submitted
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL", name="fk_arena_adc_user"),
        nullable=True,
    )
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class ArenaADCheck(db.Model):
    __tablename__ = "arena_ad_checks"
    __table_args__ = (
        db.UniqueConstraint(
            "round_id", "team_id", "service_id", name="uq_arena_adk_round_team_svc"
        ),
        db.CheckConstraint(_in("status", CHECK_STATUSES), name="ck_arena_adk_status"),
        db.Index("ix_arena_adk_event_team", "event_id", "team_id"),
    )

    id = db.Column(BigInt, primary_key=True, autoincrement=True)
    event_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_events.id", ondelete="CASCADE", name="fk_arena_adk_event"),
        nullable=False,
    )
    round_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_ad_rounds.id", ondelete="CASCADE", name="fk_arena_adk_round"
        ),
        nullable=False,
    )
    team_id = db.Column(
        db.Integer,
        db.ForeignKey("arena_teams.id", ondelete="CASCADE", name="fk_arena_adk_team"),
        nullable=False,
    )
    service_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "arena_ad_services.id", ondelete="CASCADE", name="fk_arena_adk_service"
        ),
        nullable=False,
    )
    status = db.Column(db.String(16), nullable=False)
    detail = db.Column(db.String(255), nullable=True)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)

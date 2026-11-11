"""Migration tests.

* Schema produced by the Alembic revision matches the SQLAlchemy models.
* On MariaDB/MySQL (TESTING_DATABASE_URL=mysql+pymysql://...), up/down/up on
  a populated native database leaves every native table byte-identical
  (CHECKSUM TABLE) and is idempotent.
"""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from CTFd.models import (
    AudienceMembers,
    Audiences,
    Configs,
    ModuleAudienceAccess,
    Modules,
    db,
)
from CTFd.plugins.arena import dbmigrate
from CTFd.plugins.arena.core import schema as schema_guard
from CTFd.plugins.arena.models import ArenaEvent, ArenaRegistration, ArenaRole
from tests.arena.helpers import create_arena_ctfd, gen_event, gen_registration
from tests.helpers import (
    create_ctfd,
    destroy_ctfd,
    gen_award,
    gen_challenge,
    gen_fail,
    gen_flag,
    gen_hint,
    gen_solve,
    gen_team,
    gen_unlock,
    gen_user,
)

ARENA = set(dbmigrate.ARENA_TABLES)


def _is_mysql(app):
    return app.config["SQLALCHEMY_DATABASE_URI"].startswith("mysql")


def _arena_diffs(conn):
    def include_object(obj, name, type_, reflected, compare_to):
        # Filter BOTH sides (reflected DB and model metadata) to Arena tables.
        table = obj if type_ == "table" else getattr(obj, "table", None)
        table_name = name if type_ == "table" else getattr(table, "name", None)
        return table_name in ARENA if table_name else True

    mc = MigrationContext.configure(
        conn, opts={"compare_type": True, "include_object": include_object}
    )
    return compare_metadata(mc, db.metadata)


def test_migration_schema_matches_models():
    app = create_arena_ctfd()
    try:
        with app.app_context():
            if dbmigrate.is_sqlite(app):
                # Tables came from create_all(); rebuild them via the migration.
                dbmigrate.downgrade("base")
                assert dbmigrate.existing_arena_tables() == []
                rev = dbmigrate._script().get_revision(dbmigrate.head_revision())
                with db.engine.connect() as conn:
                    ctx = MigrationContext.configure(conn)
                    with ctx.begin_transaction():
                        rev.module.upgrade(op=Operations(ctx))
            with db.engine.connect() as conn:
                diffs = _arena_diffs(conn)
            assert diffs == [], diffs
            insp = inspect(db.engine)
            names = {
                u["name"] for u in insp.get_unique_constraints("arena_registrations")
            }
            assert "uq_arena_reg_event_user" in names
    finally:
        destroy_ctfd(app)


# ---------------------------------------------------------------- MariaDB only


def _populate_native(app):
    """Representative native data across the tables Arena must never touch."""
    gen_challenge(db, name="c1")
    gen_challenge(db, name="c2", value=200)
    gen_flag(db, challenge_id=1, content="flag{1}")
    gen_flag(db, challenge_id=2, content="^flag.*$", type="regex")
    gen_hint(db, challenge_id=1, content="hint", cost=10)
    team = gen_team(db, name="native-team", member_count=2)
    users = [gen_user(db, name=f"n{i}", email=f"n{i}@example.com") for i in range(5)]
    for u in users:
        gen_solve(db, user_id=u.id, challenge_id=1)
        gen_fail(db, user_id=u.id, challenge_id=2)
    gen_award(db, user_id=users[0].id, value=50)
    gen_unlock(db, user_id=users[0].id, target=1)
    m = Modules(name="native-module")
    a = Audiences(name="native-audience")
    db.session.add_all([m, a])
    db.session.commit()
    db.session.add(ModuleAudienceAccess(module_id=m.id, audience_id=a.id))
    db.session.add(AudienceMembers(audience_id=a.id, user_id=users[1].id))
    db.session.commit()
    return team


def _native_fingerprint():
    tables = [
        t
        for t in inspect(db.engine).get_table_names()
        if t not in ARENA and t not in ("config", "alembic_version")
    ]
    sums = {}
    with db.engine.connect() as conn:
        for t in sorted(tables):
            row = conn.execute(text(f"CHECKSUM TABLE `{t}`")).fetchone()
            count = conn.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar()
            sums[t] = (row[1], count)
    configs = {
        c.key: c.value for c in Configs.query.all() if c.key != dbmigrate.VERSION_KEY
    }
    with db.engine.connect() as conn:
        core_rev = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()
    return sums, configs, core_rev


@pytest.fixture
def mysql_app():
    app = create_ctfd(enable_plugins=True)  # NOTE: no Arena migration yet
    if not _is_mysql(app):
        destroy_ctfd(app)
        pytest.skip("requires TESTING_DATABASE_URL=mysql+pymysql://...")
    yield app
    destroy_ctfd(app)


def test_mariadb_startup_does_not_create_arena_tables(mysql_app):
    with mysql_app.app_context():
        assert dbmigrate.existing_arena_tables() == []
        assert dbmigrate.current_revision() is None
        schema_guard.reset_cache()
    r = mysql_app.test_client().get("/arena/readyz")
    assert r.status_code == 503
    assert r.get_json()["checks"]["schema"] is False


def test_mariadb_up_down_up_preserves_native_data(mysql_app):
    with mysql_app.app_context():
        _populate_native(mysql_app)
        before = _native_fingerprint()
        assert sum(c for _, c in before[0].values()) > 0

        # UP
        assert dbmigrate.upgrade() == dbmigrate.head_revision()
        assert set(dbmigrate.existing_arena_tables()) == ARENA
        assert dbmigrate.schema_state()[0] is True
        assert _native_fingerprint() == before
        # idempotent
        assert dbmigrate.upgrade() == dbmigrate.head_revision()

        # Arena data lives alongside native data
        e = gen_event("migr")
        gen_registration(e.id, 2)
        db.session.add(ArenaRole(user_id=2, event_id=e.id, role="auditor"))
        db.session.commit()
        db.session.close()

        # DOWN to base
        assert dbmigrate.downgrade("base") is None
        assert dbmigrate.existing_arena_tables() == []
        assert dbmigrate.current_revision() is None
        assert _native_fingerprint() == before

        # UP again
        assert dbmigrate.upgrade() == dbmigrate.head_revision()
        assert set(dbmigrate.existing_arena_tables()) == ARENA
        assert ArenaEvent.query.count() == 0
        assert ArenaRegistration.query.count() == 0
        assert _native_fingerprint() == before
        with db.engine.connect() as conn:
            assert _arena_diffs(conn) == []

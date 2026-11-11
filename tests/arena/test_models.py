import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

# MariaDB reports CHECK violations as error 4025, which PyMySQL maps to
# OperationalError; SQLite raises IntegrityError.
CHECK_VIOLATION = (IntegrityError, OperationalError)

from CTFd.models import Modules, Users, db
from CTFd.plugins.arena.models import ArenaEvent, ArenaRegistration, ArenaRole
from tests.arena.helpers import (
    create_arena_ctfd,
    gen_event,
    gen_registration,
    gen_role,
    make_user,
)
from tests.helpers import destroy_ctfd


@pytest.fixture
def app():
    app = create_arena_ctfd()
    yield app
    destroy_ctfd(app)


def test_defaults(app):
    with app.app_context():
        e = ArenaEvent(slug="x", name="X")
        db.session.add(e)
        db.session.commit()
        assert (e.type, e.status, e.visibility, e.team_mode) == (
            "jeopardy",
            "draft",
            "public",
            "individual",
        )
        assert e.registration_open is False and e.ad_enabled is False
        assert e.created_at is not None and e.updated_at is not None


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "koth"),
        ("status", "live"),
        ("visibility", "secret"),
        ("team_mode", "duo"),
    ],
)
def test_python_validators_reject_bad_enums(app, field, value):
    with app.app_context():
        with pytest.raises(ValueError):
            ArenaEvent(slug="x", name="X", **{field: value})


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE arena_events SET status='live'",
        "UPDATE arena_events SET type='koth'",
        "UPDATE arena_events SET visibility='secret'",
        "UPDATE arena_events SET team_mode='duo'",
    ],
)
def test_db_check_constraints_reject_bad_enums(app, sql):
    """Even raw SQL that bypasses the ORM cannot store invalid states."""
    with app.app_context():
        gen_event("e")
        with pytest.raises(CHECK_VIOLATION):
            db.session.execute(text(sql))
            db.session.commit()
        db.session.rollback()


def test_registration_check_constraint(app):
    with app.app_context():
        e = gen_event("e")
        uid = make_user("u")
        gen_registration(e.id, uid)
        with pytest.raises(CHECK_VIOLATION):
            db.session.execute(text("UPDATE arena_registrations SET status='vip'"))
            db.session.commit()
        db.session.rollback()


def test_slug_unique(app):
    with app.app_context():
        gen_event("same")
        with pytest.raises(IntegrityError):
            gen_event("same")
        db.session.rollback()


def test_one_registration_per_user_per_event(app):
    with app.app_context():
        a, b = gen_event("a").id, gen_event("b").id
        uid = make_user("u")
        gen_registration(a, uid)
        gen_registration(b, uid)  # multiple events per user are fine
        with pytest.raises(IntegrityError):
            gen_registration(a, uid, status="pending")
        db.session.rollback()
        assert ArenaRegistration.query.filter_by(user_id=uid).count() == 2


def test_role_unique_per_event(app):
    with app.app_context():
        a = gen_event("a").id
        uid = make_user("u")
        gen_role(uid, "analyst", a)
        with pytest.raises(IntegrityError):
            gen_role(uid, "analyst", a)
        db.session.rollback()


def test_deleting_event_cascades_to_registrations_and_roles(app):
    with app.app_context():
        e = gen_event("a")
        uid = make_user("u")
        gen_registration(e.id, uid)
        gen_role(uid, "analyst", e.id)
        db.session.delete(e)
        db.session.commit()
        assert ArenaRegistration.query.count() == 0
        assert ArenaRole.query.count() == 0
        assert db.session.get(Users, uid) is not None  # native user untouched


def test_deleting_user_cascades_to_their_registrations(app):
    with app.app_context():
        e = gen_event("a")
        uid = make_user("u")
        gen_registration(e.id, uid)
        db.session.execute(text("DELETE FROM users WHERE id = :i"), {"i": uid})
        db.session.commit()
        assert ArenaRegistration.query.count() == 0
        assert db.session.get(ArenaEvent, e.id) is not None


def test_sealed_module_cannot_be_deleted_while_referenced(app):
    with app.app_context():
        m = Modules(name="sealed")
        db.session.add(m)
        db.session.commit()
        gen_event("a", sealed_module_id=m.id)
        with pytest.raises(IntegrityError):
            db.session.execute(text("DELETE FROM modules WHERE id = :i"), {"i": m.id})
            db.session.commit()
        db.session.rollback()
        with pytest.raises(IntegrityError):
            gen_event("b", sealed_module_id=m.id)  # one event per sealed module
        db.session.rollback()

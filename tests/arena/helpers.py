"""Shared helpers for Arena tests.

Arena tests run on SQLite by default and on MariaDB/MySQL when
TESTING_DATABASE_URL points at one (native CTFd test convention). On a real
server the Arena schema is applied explicitly through the migration runner,
exactly as an operator would with ``flask arena migrate``.
"""

import datetime

from CTFd.models import Users, db
from CTFd.plugins.arena import dbmigrate
from CTFd.plugins.arena.core import schema as schema_guard
from CTFd.plugins.arena.models import ArenaEvent, ArenaRegistration, ArenaRole
from tests.helpers import create_ctfd, gen_user, setup_ctfd


def create_arena_ctfd(blueprints=(), **kwargs):
    """Create a test CTFd with Arena loaded.

    ``blueprints`` are registered before setup, because Flask refuses new
    routes once the app has served its first request.
    """
    kwargs.setdefault("enable_plugins", True)
    app = create_ctfd(setup=False, **kwargs)
    for bp in blueprints:
        app.register_blueprint(bp)
    app = setup_ctfd(app, user_mode=kwargs.get("user_mode", "users"))
    with app.app_context():
        if not dbmigrate.is_sqlite(app):
            dbmigrate.upgrade()
        schema_guard.reset_cache()
    return app


def now():
    return datetime.datetime.utcnow()


def gen_event(slug="event", **kwargs):
    defaults = {
        "name": slug.title(),
        "type": "jeopardy",
        "status": "running",
        "visibility": "public",
        "starts_at": now() - datetime.timedelta(hours=1),
        "ends_at": now() + datetime.timedelta(hours=5),
    }
    defaults.update(kwargs)
    event = ArenaEvent(slug=slug, **defaults)
    db.session.add(event)
    db.session.commit()
    return event


def gen_registration(event_id, user_id, status="approved", source="self"):
    reg = ArenaRegistration(
        event_id=event_id, user_id=user_id, status=status, source=source
    )
    db.session.add(reg)
    db.session.commit()
    return reg


def gen_role(user_id, role, event_id=None):
    r = ArenaRole(user_id=user_id, role=role, event_id=event_id)
    db.session.add(r)
    db.session.commit()
    return r


def make_user(name, **kwargs):
    kwargs.setdefault("email", f"{name}@examplectf.com")
    user = gen_user(db, name=name, password="password", **kwargs)
    return user.id


def admin_user():
    return Users.query.filter_by(type="admin").first()

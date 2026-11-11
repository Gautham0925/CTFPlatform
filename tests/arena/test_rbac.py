import pytest

from CTFd.models import Users, db
from CTFd.plugins.arena.core import rbac
from CTFd.plugins.arena.models import ROLES, ArenaRole
from tests.arena.helpers import (
    admin_user,
    create_arena_ctfd,
    gen_event,
    gen_role,
    make_user,
)
from tests.helpers import destroy_ctfd


@pytest.fixture
def app():
    app = create_arena_ctfd()
    yield app
    destroy_ctfd(app)


def test_every_role_has_permissions_and_superadmin_only_perms_are_unassigned():
    assert set(rbac.ROLE_PERMISSIONS) == set(ROLES)
    for perms in rbac.ROLE_PERMISSIONS.values():
        assert rbac.PERM_EVENTS_CREATE not in perms
        assert rbac.PERM_ROLES_MANAGE not in perms


def test_permission_matrix(app):
    with app.app_context():
        a = gen_event("a").id
        b = gen_event("b").id
        users = {}
        for role in ROLES:
            uid = make_user(role)
            gen_role(uid, role, event_id=a)
            users[role] = db.session.get(Users, uid)
        for role, user in users.items():
            for perm in rbac.ALL_PERMISSIONS:
                want = perm in rbac.ROLE_PERMISSIONS[role]
                assert rbac.has_permission(user, perm, event_id=a) is want, (role, perm)
                # A role scoped to event A grants nothing on event B.
                assert rbac.has_permission(user, perm, event_id=b) is False
                # ...nor globally.
                assert rbac.has_permission(user, perm, event_id=None) is False


def test_superadmin_has_every_permission(app):
    with app.app_context():
        a = gen_event("a").id
        admin = admin_user()
        for perm in rbac.ALL_PERMISSIONS:
            assert rbac.has_permission(admin, perm, event_id=a)
            assert rbac.has_permission(admin, perm)


def test_global_role_applies_to_all_events(app):
    with app.app_context():
        a, b = gen_event("a").id, gen_event("b").id
        uid = make_user("gaud")
        gen_role(uid, "auditor", event_id=None)
        u = db.session.get(Users, uid)
        assert rbac.has_permission(u, rbac.PERM_AUDIT_READ, event_id=a)
        assert rbac.has_permission(u, rbac.PERM_AUDIT_READ, event_id=b)
        assert not rbac.has_permission(u, rbac.PERM_EVENT_MANAGE, event_id=a)


def test_banned_users_lose_roles_and_superadmin(app):
    with app.app_context():
        a = gen_event("a").id
        uid = make_user("ea")
        gen_role(uid, "event_admin", event_id=a)
        u = db.session.get(Users, uid)
        u.banned = True
        db.session.commit()
        assert not rbac.has_permission(u, rbac.PERM_EVENT_MANAGE, event_id=a)
        admin = admin_user()
        admin.banned = True
        assert not rbac.is_superadmin(admin)
        db.session.rollback()


def test_unknown_permission_fails_closed(app):
    with app.app_context():
        with pytest.raises(rbac.UnknownPermission):
            rbac.has_permission(admin_user(), "events.delete_everything")


def test_grant_is_idempotent_including_global_roles(app):
    with app.app_context():
        uid = make_user("g")
        r1 = rbac.grant_role(uid, "auditor")
        r2 = rbac.grant_role(uid, "auditor")
        assert r1.id == r2.id
        assert ArenaRole.query.filter_by(user_id=uid).count() == 1
        assert rbac.revoke_role(uid, "auditor") == 1
        with pytest.raises(ValueError):
            rbac.grant_role(uid, "god")


def test_none_user_has_nothing(app):
    with app.app_context():
        assert rbac.roles_for(None, 1) == set()
        assert rbac.has_permission(None, rbac.PERM_EVENT_VIEW_ADMIN, 1) is False

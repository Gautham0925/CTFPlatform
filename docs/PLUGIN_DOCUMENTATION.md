# Arena Plugin — Developer Guide

Package: `CTFd/plugins/arena/`. Loaded by CTFd's `init_plugins()` (skipped when `SAFE_MODE=true`).

## Layout (Phase 1)

| Path | Responsibility |
|---|---|
| `__init__.py` | `load(app)`: settings, models, CLI, health blueprint. No DDL on servers. |
| `config.py` | `ArenaSettings` (app config → env → default), validated |
| `models.py` | `ArenaEvent`, `ArenaRegistration`, `ArenaRole` |
| `dbmigrate.py` | Explicit upgrade (via native runner) / downgrade / schema state |
| `migrations/` | Alembic revisions (each has `upgrade(op)` and `downgrade(op)`) |
| `core/context.py` | Event resolution, visibility, `scoped_query`, `scoped_get_or_404` |
| `core/authz.py` | Route guards |
| `core/rbac.py` | Roles, permissions, grant/revoke |
| `core/schema.py` | `arena_blueprint()` factory with 503 schema guard |
| `health.py` | `/arena/healthz`, `/arena/readyz` |
| `integrity.py`, `cli.py` | `flask arena …` |

## Settings

| Key | Default | Notes |
|---|---|---|
| `ARENA_ENABLED` | `true` | `false` → only the CLI is registered |
| `ARENA_CERTIFICATES_ENABLED` | `false` | Certificates are optional (Phase 12) |
| `ARENA_RUNNER_MODE` | `mock` | Only `mock` is accepted until Phase 8 |
| `ARENA_SCHEMA_CHECK_TTL` | `30` | Seconds a positive schema check is cached per process |

## Writing an event-scoped endpoint (mandatory pattern)

```python
from CTFd.plugins.arena.core.schema import arena_blueprint
from CTFd.plugins.arena.core.authz import event_participant_required, event_permission_required
from CTFd.plugins.arena.core.context import scoped_get_or_404
from CTFd.plugins.arena.core import rbac

bp = arena_blueprint("arena_things", __name__)

@bp.route("/api/v1/arena/events/<int:event_id>/things/<int:thing_id>")
@event_participant_required          # event comes from the URL only
def get_thing(event, thing_id):      # event_id is replaced by the resolved event
    thing = scoped_get_or_404(Thing, thing_id, event)   # WHERE event_id = event.id
    ...

@bp.route("/api/v1/arena/events/<int:event_id>/admin/things", methods=["POST"])
@event_permission_required(rbac.PERM_EVENT_MANAGE)
def create_thing(event):
    ...
```

Rules, enforced by `tests/arena/test_static_rules.py`:
- Never authorize with an event id from `request.args`, `request.form` or JSON.
- Never use `Model.query.get(id)` in Arena code. Use `scoped_get_or_404`.
- Never build SQL with f-strings or `%`.
- Never use `bypass_csrf_protection`.

### Guard semantics

| Guard | Anonymous | Event not visible | Visible, not allowed | Allowed |
|---|---|---|---|---|
| `event_view` | public/unlisted → 200 | 404 | — | 200 |
| `event_participant_required` | 401 | 404 | 403 (no approved registration, natively banned, unverified email when required) | 200 |
| `event_permission_required(p)` | 401 | 404 | 403 | 200 |
| `global_permission_required(p)` | 401 | — | 403 | 200 |

Visibility: superadmins and role holders see every event (including drafts). Everyone else sees only `published/running/paused/ended/archived` events; `private` events additionally require a `pending` or `approved` registration. A guard placed on a route without `<event_id>` raises `GuardMisconfigured` (fails closed).

### Roles

| Role | Permissions |
|---|---|
| `event_admin` | view_admin, manage, registrations, teams, challenges, scoring, ad.operate, certificates, audit |
| `ad_operator` | view_admin, ad.operate |
| `cert_manager` | view_admin, certificates.manage |
| `auditor` | view_admin, audit.read |
| native admin | everything, plus superadmin-only `events.create` and `roles.manage` |

A role with `event_id = NULL` applies to all events. Banned users hold no permissions.

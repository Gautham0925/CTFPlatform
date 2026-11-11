# Development guide

## Layout

```
CTFd/plugins/arena/          the Arena plugin (all platform code)
  __init__.py                load(): settings, CLI, blueprints, hooks, UI
  config.py                  ArenaSettings + ARENA_* env import
  core/                      authz guards, RBAC, context, schema guard, rate limiter
  models/                    SQLAlchemy models (arena_* tables only)
  services/                  business logic (events, registration, teams, challenges,
                             submissions, scoring, scoreboard, mixed, jobs, activity,
                             analytics, instances, ad, labs, certificates, audit)
  api/                       JSON blueprints under /api/v1/arena
  ui/ templates/ assets/     server-rendered shells + vanilla JS (no build step)
  execution/                 runner client (mock + HTTPS/HMAC), signing copy
  labs/                      provider interface, mock, generic HTTP, SSRF guard, crypto
  tasks.py worker.py         job handlers, periodic schedules, worker loop
  migrations/                Alembic revisions a7e1f00d0001..0013
runner/                      standalone runner service (separate host)
deploy/                      compose profiles, nginx, MariaDB/Redis config, scripts, systemd
tests/arena/                 tests (+ e2e/, load/, security/, tools/)
```

## Rules that keep Arena safe

1. **Never modify native CTFd code.** Extend it through plugin hooks.
   `tests/arena/test_native_compat.py` and the native suite run with Arena
   force-loaded catch regressions.
2. **Event scoping:** an API handler gets its `event` from a guard
   (`event_view`, `event_participant_required`,
   `event_permission_required(PERM)`), never from the request body. Load
   child objects with `scoped_get_or_404(Model, id, event)`.
   `test_static_rules.py` forbids unscoped `.query.get(...)`.
3. **New routes** must use a guard. `test_authz_matrix.py` fails if an
   unguarded route answers 2xx and is not on its allow-list.
4. **Native rows** for event content go into the event's sealed module,
   using `ensure_sealed_module`.
5. **Concurrency:** use DB constraints and compare-and-set updates, not
   read-modify-write. When a transaction touches `arena_event_challenges`
   and solves/ledger rows, update the challenge row **first**.
6. **Redis** is optional for Arena's own logic. Go through `arena_cache`,
   which has timeouts, a circuit breaker and a DB fallback.
7. **No new runtime dependencies** without a free/open-source licence. The
   platform must never import the Docker SDK.

## Local setup

```bash
uv sync --frozen --group dev     # creates .venv from uv.lock (Python 3.11), incl. pytest + ruff
```

```bash
.venv/bin/python -m pytest -q tests/arena            # SQLite, ~2 min
```

```bash
.venv/bin/flask run                                   # http://127.0.0.1:4000, plugin auto-loads
```

To run the worker locally:

```bash
.venv/bin/flask arena worker --poll 1
```

## Schema changes

1. Change the models in `CTFd/plugins/arena/models/`.
2. Create a revision in `CTFd/plugins/arena/migrations/` (Alembic,
   `down_revision` = the previous head). Generate it against a MariaDB at the
   previous head. Never drop an index that backs a foreign key before
   dropping the table (MariaDB error 1553). Guard column alterations for
   SQLite.
3. `TESTING_DATABASE_URL=mysql+pymysql://… pytest tests/arena/test_migrations.py`
   runs upgrade, downgrade and upgrade again, and checks that native rows are unchanged.
4. Regenerate the reference docs:
   `python tests/arena/tools/gen_reference_docs.py routes.md schema.md`.

## Adding a background job

```python
@jobs.handler("myfeature.do", max_attempts=5, lease_seconds=60)
def do_it(payload, job): ...
```

Enqueue with `jobs.enqueue("myfeature.do", {...}, dedupe_key=...)`. For a
periodic job, add `periodic("name", seconds, "myfeature.do")` in `tasks.py`; only the
worker holding the scheduler lease enqueues it.

## Style

`ruff check` and `ruff format` (the configuration is the repository's). The
JavaScript is plain ES5, has no build step, and builds DOM nodes with `h()`.
Only server-sanitized `*_html` fields may be set as HTML (enforced by
`test_ui.py`).

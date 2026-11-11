# Phase 1 Report — Foundation

Branch `arena/phase-1-foundation` (from `arena/phase-0-analysis` @ `4d1a69f5`). Date 2026-11-11.
Environment: Ubuntu 24.04 container, 4 vCPU / 15 GB, Python 3.11.18, MariaDB 10.11.14 (native install, started with upstream's `utf8mb4_unicode_ci` flags), SQLite (in-memory) for the default suite.

## 1. Files

**Created — plugin** (`CTFd/plugins/arena/`): `__init__.py`, `config.py`, `models.py`, `dbmigrate.py`, `cli.py`, `health.py`, `integrity.py`, `core/__init__.py`, `core/context.py`, `core/authz.py`, `core/rbac.py`, `core/schema.py`, `migrations/a7e1f00d0001_arena_foundation.py`

**Created — tests** (`tests/arena/`): `__init__.py`, `helpers.py`, `test_authz.py`, `test_rbac.py`, `test_models.py`, `test_config.py`, `test_schema_and_health.py`, `test_cli.py`, `test_migrations.py`, `test_static_rules.py`, `test_native_compat.py`

**Created — docs**: `MIGRATION_GUIDE.md`, `PLUGIN_DOCUMENTATION.md`, `CHANGELOG.md`, `PHASE_1_REPORT.md`

**Modified**: `docs/DATABASE_DESIGN.md`, `docs/IMPLEMENTATION_PLAN.md`, `docs/TESTING_PLAN.md` (status/deviations/baseline). `.gitignore` gets one line, `!CTFd/plugins/arena/`, because upstream ignores all non-bundled plugins.

**Core CTFd application files modified: none** (verified with `git diff --name-only` outside `docs/` and `.gitignore`).

## 2. Database — revision `a7e1f00d0001`

| Table | Key constraints |
|---|---|
| `arena_events` | `uq_arena_events_slug`, `uq_arena_events_sealed_module`; FK `sealed_module_id → modules.id` **RESTRICT**; FK `created_by → users.id` SET NULL; CHECK on `type`, `status`, `visibility`, `team_mode`; index `(status, starts_at)` |
| `arena_registrations` | `uq_arena_reg_event_user (event_id, user_id)`; FK event CASCADE, user CASCADE, approver SET NULL; CHECK on `status`, `source`; indexes `(event_id, status)`, `(user_id)` |
| `arena_roles` | `uq_arena_roles_user_event_role`; FK user CASCADE, event CASCADE, granter SET NULL; CHECK on `role`; index `(event_id)` |

- Timestamps are `DATETIME(6)` on MariaDB and tables inherit `utf8mb4_unicode_ci`. This was caught and fixed during the phase: an explicit `mysql_charset` had produced `general_ci`.
- The version is stored in native `config.arena_alembic_version`, using the native plugin convention.
- Deviations from Phase 0 design: enums are `VARCHAR` + `CHECK`; `arena_jobs` → Phase 6; `scoring_config_id` → Phase 5; `secret_ref` → Phase 9.

## 3. Architecture decisions implemented

| Decision | Implementation |
|---|---|
| Plugin-based, no core edits (req 1) | Single plugin package; zero core application changes |
| Migrations separate from startup (rules) | `load()` never runs DDL on MySQL/MariaDB. Startup only checks schema and logs a warning; Arena routes return 503 via `arena_blueprint()` until `flask arena migrate` runs. Verified on MariaDB: a fresh app start creates no Arena tables. |
| Reversible migrations | `dbmigrate.downgrade()`, which the native helper lacks; `flask arena db-downgrade REV\|base --yes` |
| Strict event authorization | Event comes from the URL only; 404 for invisible events; DB-checked registration/roles on every request; `scoped_get_or_404` for child objects; guards fail closed if misapplied |
| RBAC (D9 / brief §12) | `event_admin`, `ad_operator`, `cert_manager`, `auditor`, scoped per event or global; superadmin-only `events.create` and `roles.manage` (no escalation path) |
| Certificates optional, off by default (req 7) | `ARENA_CERTIFICATES_ENABLED=false` |
| Mock runner first (req 10) | `ARENA_RUNNER_MODE` accepts only `mock` until Phase 8 |
| No paid software (req 8) | No new dependencies added at all |
| Health/readiness | `/arena/healthz` (no I/O), `/arena/readyz` (DB, cache, schema; booleans only; 503 on failure, no 500) |

Not in Phase 1 by design: event sealing (sealed modules are *created* in Phase 2; the column, FK and integrity check exist now), DB worker (Phase 6), ETag scoreboards (Phase 5), labs (Phase 11).

## 4. Tests executed (actual results)

| Suite | Backend | Result |
|---|---|---|
| Arena (`tests/arena`) | SQLite | **245 passed, 2 skipped** (the 2 skips are MariaDB-only tests); also clean in random order (`pytest-randomly`) |
| Arena (`tests/arena`) | MariaDB 10.11.14 | **247 passed, 0 skipped**; no leftover test databases |
| Native, as shipped | SQLite | **742 passed, 2 skipped** |
| Native, plugins forced on | SQLite | **742 passed, 2 skipped** |
| Lint | `ruff check` + `ruff format --check` on Arena files | clean |

## 5. Native CTFd compatibility

All runs: `pytest -n 4 -p no:randomly`, SQLite, full locked dev dependency group (includes `moto`).

| Run | Tree | Plugins | Result |
|---|---|---|---|
| Baseline | Phase 0 commit `4d1a69f5` (clean worktree) | native default (`SAFE_MODE` except tests that enable plugins) | 742 passed, 2 skipped, 0 failed |
| 1 | Phase 1 branch | native default | **742 passed, 2 skipped, 0 failed** |
| 2 | Phase 0 commit (no Arena) | **forced on for every test** | 742 passed, 2 skipped, 0 failed |
| 3 | Phase 1 branch | **forced on for every test** (Arena loaded) | **742 passed, 2 skipped, 0 failed** |

Runs 2 and 3 have identical (empty) failure sets. Forcing uses a small pytest plugin that sets `create_ctfd(enable_plugins=True)` as the default. A control run confirmed Arena is loaded under the plugin (`Loaded module … CTFd.plugins.arena`) and not without it. So every native test passes **with Arena active**.

Note: an earlier run showed 10 failures. That run was started while `CTFd/plugins/arena/` was a half-created empty directory, so plugin-enabled native tests failed to import it. The clean-worktree baseline above replaced it, and the condition can't recur once the package exists.

Additional Arena-side compatibility tests (`test_native_compat.py`): with Arena loaded, the native challenge list, attempt (correct flag), scoreboard and `/healthcheck` all work; `SAFE_MODE` loads no Arena; `ARENA_ENABLED=false` registers only the CLI.

## 6. Migration up/down/up

**MariaDB 10.11.14** (`test_mariadb_up_down_up_preserves_native_data`, plus manual CLI runs):
1. Fresh app start on MariaDB → no Arena tables, `current=None`, `/arena/readyz` → 503 with `schema:false`.
2. Native data populated: challenges, static+regex flags, hints, a team with members, users, solves, fails, awards, unlocks, modules/audiences/members.
3. Fingerprint = `CHECKSUM TABLE` + row count for every native table, all config rows except the Arena key, and the core Alembic revision.
4. **Up** → 3 tables, `current=head`, fingerprint identical. Second `upgrade()` is a no-op.
5. Arena rows inserted (event, registration, role).
6. **Down to base** → Arena tables gone, version cleared, **fingerprint identical**.
7. **Up again** → tables recreated empty, fingerprint identical, model/DB schema diff empty.

Schema parity: `compare_metadata` (with `compare_type=True`) between the migrated DB and the models returns **no differences** on SQLite and MariaDB. A negative control (adding a column to the model only) is detected.

CLI on MariaDB (manual): `db-current` → `up_to_date:false`; `migrate` → `None → a7e1f00d0001`; `db-downgrade base` without `--yes` → refused (exit 2); with `--yes` → `a7e1f00d0001 → None`; `migrate` → back to head.

## 7. Security & IDOR results

- **Guard matrix:** 11 actor classes × 4 events (public, private, draft, unlisted) × 4 guards = **176 cases**, all matching an independent oracle. Actors: anonymous, outsider, pending, approved in A, approved in B, approved everywhere, registration-banned, event_admin A, auditor A, global auditor, superadmin.
- **Smuggled `event_id`** in query string and JSON is ignored: the B member stays 403 on A, and the handler only sees the URL event. An event admin of A can't manage D by smuggling A's id.
- **Cross-event child object:** a valid registration id from event B under event A's URL → 404.
- **Nonexistent and non-integer event ids** (including `1 OR 1=1`) → 404.
- **Natively banned user** → 403 on every route. **Revoked registration** takes effect on the next request (DB, not session).
- **CSRF:** native nonce check covers Arena POSTs (no token → 403, forged token → 403).
- **RBAC:** full role × permission matrix; event-scoped roles grant nothing on other events or globally; banned users lose all permissions; superadmin-only permissions belong to no role; unknown permission names fail closed (at decoration time and at call time).
- **DB-level integrity:** CHECK constraints reject invalid states even via raw SQL (SQLite and MariaDB); unique registration per event/user; a sealed module can't be deleted while referenced, nor shared by two events.
- **Static rules:** no `.query.get(`, no string-built SQL, no CSRF bypass, guards never read `request.args/form/json`, `load()` contains no migration calls.

## 8. Outstanding issues

1. **Before setup completes, `/arena/healthz` and `/arena/readyz` redirect to `/setup`.** Native `needs_setup` allowlists only native endpoints. Fixing this would be a core change. Use native `/healthcheck` for liveness before setup.
2. **Native CTFd needs Redis/cache for sessions.** A full Redis outage breaks native request handling before Arena code runs. Arena's readiness reports a cache failure correctly, but Arena can't make native sessions survive it. Scoreboard Redis-failure handling is Phase 5 work.
3. **The plugin is not loaded under `SAFE_MODE=true`.** That's native behaviour, and it also hides the `flask arena` CLI. Run migrations with `SAFE_MODE` off.
4. **Uniqueness of global roles** (`event_id NULL`) is enforced in `grant_role()` and reported by `check-integrity`, not by the DB (SQL UNIQUE treats NULLs as distinct).
5. **Guards cost about 3 small indexed queries per request** (event, roles, registration). That's fine for 100 users; per-request memoization will be revisited at Phase 5 load testing.
6. **No git remote for your repository yet.** Commits are local only.
7. **MariaDB tests ran on a native install, not the compose image.** The Docker daemon is unavailable in this container. CI should use `mariadb:10.11`.

## 9. Exit criteria

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Branch/commit verified, Phase 0 docs present, clean tree | ✅ | `arena/phase-0-analysis @ 4d1a69f5`, 8 docs, empty `git status` before branching |
| 2 | Native baseline cleaned (moto) | ✅ | 742 passed, 0 errors (was 1 collection error) |
| 3 | Phase-specific branch | ✅ | `arena/phase-1-foundation` |
| 4 | Plugin skeleton, settings, models | ✅ | §1, §3 |
| 5 | Alembic chain rev 1 | ✅ (scope adjusted) | `a7e1f00d0001`; `arena_jobs` deferred to Phase 6 with the worker |
| 6 | Event context resolver, authz guards, RBAC | ✅ | §7 |
| 7 | `flask arena migrate / db-downgrade / check-integrity` | ✅ | `test_cli.py`, manual MariaDB runs |
| 8 | No migrations in gunicorn workers / app startup | ✅ | MariaDB startup test; static rule test on `load()` |
| 9 | All native tests still pass | ✅ | §5, all four runs 742/0 |
| 10 | Migration up/down/up on MariaDB with native data preserved | ✅ | §6, checksums identical |
| 11 | Guard unit tests (IDOR matrix) | ✅ | 176-case matrix + 12 targeted tests |
| 12 | No core CTFd application files modified | ✅ | only `.gitignore` (+1 line), documented |
| 13 | Docs updated | ✅ | §1 |
| 14 | Pushed to a remote | ❌ **blocked** | no remote for your repository is configured; commits are local |

Phase 1 is complete except item 14, which needs a repository from you.

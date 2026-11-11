# Arena Changelog

Changes of the Arena extension. CTFd's own history is in the root `CHANGELOG.md` (unmodified).

## 0.1.0-phase1 — Foundation (unreleased)

### Added
- `CTFd/plugins/arena` plugin: settings, models, migration runner, RBAC, event context and route guards, schema guard, health endpoints, `flask arena` CLI.
- Alembic revision `a7e1f00d0001`: `arena_events`, `arena_registrations`, `arena_roles`.
- `tests/arena/` suite (SQLite and MariaDB).
- Docs: `MIGRATION_GUIDE.md`, `PLUGIN_DOCUMENTATION.md`, `PHASE_1_REPORT.md`.

### Changed
- No CTFd application code changed.
- `.gitignore`: added `!CTFd/plugins/arena/` (upstream ignores non-bundled plugins).

### Deviations from the Phase 0 design
- `arena_jobs` moves from rev 1 to Phase 6, together with the worker that uses it (no unused schema).
- `arena_events.scoring_config_id` moves to Phase 5 (its target table doesn't exist yet).
- `arena_events.secret_ref` moves to Phase 9 (A&D).
- Enumerations are `VARCHAR` + `CHECK` + Python validation instead of SQL `ENUM`, so new values can be added without a table rebuild.

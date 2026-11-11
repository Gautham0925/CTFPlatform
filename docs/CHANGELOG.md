# Arena Changelog

Changes of the Arena extension. CTFd's own history is in the root `CHANGELOG.md` (unmodified).

## 0.2.0 — Phases 2–17 (unreleased)

### Added
- **Events** (Phase 2): lifecycle `draft → upcoming → active → paused/completed → archived`, public/unlisted/private visibility, registration windows, capacity (atomic seats), approval, invitation codes, per-event settings schema, branding, announcements.
- **Teams** (Phase 3): per-event teams, join codes, invites, captain transfer, size limits, locking.
- **Jeopardy engine** (Phase 4): event challenges on sealed native modules; static, regex and case-insensitive flags; per-entity HMAC flags; hints with costs; files; prerequisites; release windows; clone; exactly-once solves under concurrency.
- **Scoring** (Phase 5): append-only ledger, dynamic values, first blood, adjustments, freeze, cached scoreboards with weak ETags and LKG fallback, verify endpoint.
- **Worker** (Phase 6): DB job queue with leases, retries, dead letter and dedupe; DB leader lease for periodic schedules; graceful stop.
- **Activity and analytics** (Phase 7): Redis-buffered activity (no IP/UA/flags), hourly rollups, reports, CSV exports with formula-injection protection, retention.
- **Runner** (Phase 8): standalone service, HMAC + TLS, digest-pinned images, hardened containers, firewall script, reaper.
- **Attack & Defense** (Phase 9): services, rounds, flags, captures, checkers (on the runner), SLA scoring.
- **Mixed events** (Phase 10): versioned scoring configs (weights, tie-break) with before/after snapshots.
- **External labs** (Phase 11): provider interface, MockProvider, GenericHTTPProvider (SSRF-checked, retries, encrypted tokens).
- **Certificates** (Phase 12): optional and **disabled by default**; PDF via Pillow; public verification with code.
- **Dashboards** (Phases 13–14): participant pages under `/arena`, admin console under `/arena/admin`; no build step.
- **Deployment** (Phase 15): `deploy/` with Profiles A/B/C (compose), nginx TLS template, MariaDB/Redis config, install/upgrade/backup/restore/health scripts, runner TLS tooling, systemd units.
- **Validation** (Phase 16): Locust load suite (`tests/arena/load/`), live security probes (`tests/arena/security/`), dynamic authorization sweep over every route, deployment tests.
- CLI: `flask arena worker`, `worker-health`, `jobs stats|retry|purge|recover`, `check-integrity` (extended).
- Docs: the full Phase 17 set (see `docs/README.md`).

### Fixed during Phases 15–16 (found by deployment, load and fault tests)
- `ARENA_*` environment variables were ignored (only CTFd-known keys reach `app.config`). They are now imported at load; `ARENA_SECRET_KEY` must be ≥ 32 chars. **See MIGRATION_GUIDE "Notes for 0.2.0".**
- Arena endpoints rate-limited per IP; now per account (venue NAT).
- Released instances kept holding the participant's quota until their TTL expired.
- `check-integrity` / scoreboard verify failed with MariaDB error 1267 when the connection and column collations differ.
- `/arena/readyz` was answered by CTFd's setup redirect before setup.
- Deployment: Redis persistence (sessions survive restarts); nginx↔gunicorn keep-alive race that caused 502 storms; egress network for web/worker; runner CA key kept out of the platform mount.
- Worker memory leak: `flask arena worker` inherited `FLASK_ENV=development` from the repo's `.flaskenv` (debug mode), and Flask-SQLAlchemy recorded every query on the worker's single, never-ending app context. The worker now uses a context per iteration; the image and systemd units force production mode.
- Runner: Flask 3.1.3 / Werkzeug 3.1.9 (pip-audit), column allow-list in `store.update`, optional `ARENA_RUNNER_FORCE_USER`, unauthenticated `/livez`.

### Changed
- Still no CTFd application code changed (only `.gitignore`, see 0.1.0).

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

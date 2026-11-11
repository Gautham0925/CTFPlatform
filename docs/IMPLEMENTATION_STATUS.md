# Implementation Status

Updated continuously. Every result below was produced by an actual test run; counts are copied from pytest output. "Native" means the unmodified CTFd test suite (`tests/`, excluding `tests/arena`) with plugin loading **forced on** for every test, so Arena is active.

Baseline (Phase 0 tree, clean worktree): **742 passed, 2 skipped**.

| Phase | Status | Evidence |
|---|---|---|
| 0 Analysis | ✅ | docs/PROJECT_ANALYSIS.md … TESTING_PLAN.md |
| 1 Foundation | ✅ | re-validated before Phase 2: Arena 245 passed/2 skipped (SQLite), 247 passed (MariaDB) |
| 2 Events & registration | ✅ | §2 |
| 3 Event teams | ✅ | §3 |
| 4 Jeopardy engine | ✅ | §4 |
| 5 Scoring & scoreboard | ✅ | §5 |
| 6 Worker & jobs | ✅ | §6 |
| 7 Activity & analytics | ✅ | §7 |
| 8 Runner | ✅ | §8 |
| 9 Attack & Defense | ✅ | §9 |
| 10 Mixed scoring | ✅ | §10 |
| 11 External labs | ✅ | §11 |
| 12 Certificates (optional, off by default) | ✅ | §12 |
| 13–14 Dashboards | ✅ | §13 |
| 15 Deployment profiles | ✅ | §15 |
| 16 Load / security / reliability | ✅ with documented limits | §16, PERFORMANCE_REPORT.md, SECURITY_GUIDE.md |
| 17 Documentation | ✅ | docs/README.md |
| **Final regression** | see §Final | |

## Phase 2 — Events and registration

**Implemented**
- Lifecycle `draft → upcoming → active ⇄ paused → completed → archived` (with `completed → active` reopen and `archived → completed`). Transitions are validated server-side. `effective_phase()` applies time windows, so rules hold even without the worker. `sync_statuses()` persists time-driven transitions (worker job in Phase 6).
- Event CRUD; a sealed native module is created per event. Delete is allowed only for drafts with no registrations and no challenges; otherwise organizers archive.
- Branding (validated colours, tagline, `https://` or same-site logo URL). Closed settings schema with defaults (unknown keys rejected).
- Registration: windows, open/closed toggle, approval flow, capacity, re-registration after withdrawal, admin enrollment (`force` to exceed capacity), approve/reject/ban/unban/remove.
- Eligibility: verified email (event or native setting), email-domain allowlist, invitation codes (hashed, max uses, expiry, revocation). Private events are reachable only via `POST /events/join` with a code.
- Capacity: conditional atomic `UPDATE … seat_count < max_participants` in the registration transaction. Every status change is compare-and-set, so concurrent transitions can't move the counter twice.
- Admin audit trail (`arena_activity`) for all organizer actions.
- RBAC roles per spec: organizer, challenge_manager, scoreboard_operator, ad_operator, cert_manager, analyst. Superadmin = native admin.

**Migration** `a7e1f00d0002`: status/role value mapping (reversible; downgrade deletes the two new role kinds), `branding`/`settings`/`seat_count` (backfilled), `arena_event_invites`, `arena_registrations.invite_id`, `arena_activity`.

**Tests (actual)**
- SQLite: 290 passed, 7 skipped (MariaDB-only).
- MariaDB snapshot: **297 passed**.
- Concurrency (MariaDB): 40 users → 5-seat event = exactly 5 admitted, 35 `event_full`, counter == rows; 20 parallel registrations of one user = 1 row; 10 parallel bans = seat released once.
- Migration from previous schema (MariaDB): rev1 with old-format rows → rev2 maps statuses/roles, backfills seats, new CHECK rejects old values; downgrade restores old values; native checksums identical.
- Native (forced plugins, snapshot of Phase 2 tree): **742 passed, 2 skipped**.

**Defects found and fixed during the phase:** single-character slugs accepted (regex); Arena API 404s rendered as native HTML (Flask handler precedence; fixed by registering per-code handlers); a test-harness deadlock (connection-pool starvation before a barrier) fixed in the test.

## Phase 3 — Event teams

**Implemented**: create/join (hashed join code, regenerable)/leave/kick, direct invites (accept/reject/cancel/expire), captain transfer (CAS), team visibility (private teams unlisted and 404 to non-members), size limits (atomic counter), name uniqueness per event, locking (`lock_teams_after_start`, always after completion), ban (ban evasion by leaving or joining another team blocked), automatic removal with captain succession when a registration stops being active, organizer management (ban/unban/disband/add member/remove/set captain), integrity checks (counts, captains, event consistency, registration status).
Native `Users.team_id` is never read or written; a test asserts it stays NULL while the user captains teams in two events.

**Migration** `a7e1f00d0003`: `arena_teams`, `arena_team_members` (UNIQUE event_id+user_id), `arena_team_invites` (pending_key unique).

**Tests (actual)**: SQLite 15 passed, 3 skipped; MariaDB **18 passed**: 24 concurrent joins into a team with 3 free seats = exactly 3; one user joining 10 teams concurrently = 1 membership; concurrent captain transfers leave integrity checks clean.

**Limitation (documented):** one team per user per event, always. The spec's "unless explicitly configured" option isn't offered, because the unique constraint is the integrity guarantee the scoring model relies on.

## Phase 4 — Jeopardy engine

**Implemented**: event challenges created through CTFd's own challenge classes, forced into the event's sealed module with `state=hidden` (fail closed via `ensure_sealed_module`); standard and dynamic scoring; static, regex and case-insensitive flags; per-entity HMAC flags; hints with costs (ledger); files; tags; prerequisites; release windows; max attempts; per-entity DB-enforced submission rate; clone between events; exactly-once solves (`UNIQUE uq_arena_solve_once`, lock ordering challenge row → solve/ledger, retry on 1213/1205).
**Migration** `a7e1f00d0004`.
**Tests (actual)**: MariaDB: 40 concurrent correct submissions award points exactly once; Phase 4 snapshot Arena SQLite 334 passed / 11 skipped, MariaDB 345 passed; native forced 739 passed / 5 skipped (the 3 extra skips vs baseline are native race tests that skip on SQLite and pass on MariaDB+Redis: 39 passed).
**Defects fixed**: unsealed challenges when an event lacked a sealed module (native `/files` leak) → fail-closed sealing + integrity check; InnoDB deadlock losing correct solves → lock ordering + retry.

## Phase 5 — Scoring service and scoreboard
**Implemented**: append-only ledger `arena_score_events` (unique per source), dynamic value semantics as native, first blood, hint costs, adjustments with reason, freeze, standings with tie-break, categories, history, challenge stats, verify; Redis-cached scoreboard per event version with weak ETag/304, own Redis client (0.25 s timeouts, circuit breaker), last-known-good fallback (process + DB snapshot).
**Migration** `a7e1f00d0005`. **Tests**: MariaDB 20 passed (scoring file).

## Phase 6 — Background worker and jobs
**Implemented**: DB job queue (claim CAS, leases, exponential retry, dead letter, dedupe key cleared on finish, back-pressure per kind), DB leader lease for periodic schedules (documented deviation from a Redis lock: survives Redis outages), CAS-bucketed periodic scheduling, graceful SIGTERM, CLI `worker`, `jobs stats|retry|purge|recover`, `worker-health` (Phase 15).
**Migration** `a7e1f00d0006`. **Tests**: MariaDB 16 passed. **Defects fixed**: periodic jobs re-enqueued after completion (starvation) → schedule buckets.

## Phase 7 — Activity and analytics
**Implemented**: Redis-buffered activity (capped list, synchronous DB insert when Redis is unavailable), no IP/UA/flag text stored; hourly and per-challenge rollups; overview/timeline/challenge reports; CSV exports with formula-injection protection; retention prune.
**Migration** `a7e1f00d0007`. **Tests**: Phase 7 snapshot Arena MariaDB 386 passed; native forced 739 / 5.

## Phase 8 — Challenge runner
**Implemented**: standalone `runner/` service (Flask, HMAC signing with replay store, MockBackend, hardened DockerBackend, reaper, firewall script with egress + input chains); platform clients Mock and HTTPS/HMAC; digest-pinned images; quotas.
**Tests (actual)**: runner unit 28 passed; 6 Docker integration tests passed against a real daemon (capabilities, read-only, noexec tmpfs, pids limit, firewall blocks host/Redis/metadata).

## Phase 9 — Attack & Defense
**Implemented**: services, team services, rounds (worker tick), HMAC flags per team/service/round, flag submission with lifetime and anti-self-capture, captures, checkers executed by the runner (`/v1/ad/check`, isolated network), SLA/defense/attack scoring into the ledger.
**Migration** `a7e1f00d0009`. **Tests**: A&D + execution 20 passed (MariaDB).

## Phase 10 — Mixed scoring
**Implemented**: versioned scoring configs (weights, tie-break order) with before/after standings snapshots and diff; historical score snapshots.
**Migration** `a7e1f00d0010`. **Tests**: Phase 10 snapshot native forced 739 / 5; Arena MariaDB 409 passed, 3 failed (job tests starved by periodic jobs; fixed with `Worker(schedule=False)` in tests, re-verified 16 passed).

## Phase 11 — External labs
**Implemented**: provider interface, MockProvider, GenericHTTPProvider (contract in API_DOCUMENTATION.md; https only, SSRF check at configuration and per request, no redirects, 1 MB cap, retries for idempotent calls), Fernet-encrypted credentials, sessions with quotas, sync/expiry/health jobs.
**Migration** `a7e1f00d0011`. **Tests**: labs + mixed + migrations 24 passed (MariaDB).

## Phase 12 — Certificates (optional, disabled by default)
**Implemented**: templates, async issuing job, PDF via Pillow + bundled OpenSans, HMAC verification code, public verification page; blueprint registered only when `ARENA_CERTIFICATES_ENABLED=true`.
**Migration** `a7e1f00d0012`. **Tests**: certificates + migrations 10 passed (MariaDB).

## Phases 13–14 — Participant and admin dashboards
**Implemented**: `/arena` listing, join with code, event pages (overview, challenges, scoreboard with SVG history, team, A&D, labs, my progress), verification page; admin home, platform page (health, dead jobs, images, providers), per-event console with 13 permission-filtered tabs. Vanilla JS, no build step; HTML only from server-sanitized fields (test-enforced).
**Migration** `a7e1f00d0013` (announcements). **Tests**: `test_ui.py` 7 passed; Playwright e2e 17/17 checks (desktop + mobile screenshots reviewed; 3 rendering defects fixed).

## Phase 15 — Deployment profiles
**Implemented**: `deploy/` (Profiles A/B/C compose files, platform Dockerfile + dockerignore, entrypoint with web/worker/migrate roles, nginx TLS template, MariaDB/Redis config, gen-secrets, self-signed and runner TLS scripts, install/upgrade/backup/restore/healthcheck scripts with compose and direct modes, systemd units, env examples), worker-health CLI, runner `/livez`.
**Verified live** (not only config-checked): A, B and C brought up healthy; setup → event → solve through nginx/TLS; backup → destructive change → restore with identical table checksums and upload hashes; corrupted backup and missing `--yes` refused; C with 2 web replicas, 2 workers, TLS runner (private CA), real instance start/stop.
**Defects found and fixed**: readiness probe answered by the setup redirect; integrity check collation error (MariaDB 1267); web/worker without egress; runner CA key inside the platform mount.
**Limitation**: the sandbox blocks plain-HTTP egress and intercepts TLS in build containers, so the image was built with the Dockerfile's `apt-get` lines removed and pre-downloaded wheels; the shipped `apt-get` steps were not executed here.

## Phase 16 — Load, security and reliability
See PERFORMANCE_REPORT.md and SECURITY_GUIDE.md for all numbers. Summary (measured on a 4 vCPU VM shared with the load generator):
- 100 concurrent players, 10 min: 15,183 requests, 0 failures, API p95 ≤ 42 ms.
- Soak 30 min ×2: 38,920 requests (read-heavy) and 44,841 requests (6,133 submissions), 0 failures each; worker memory leak found and fixed.
- Login storm 150 users at 50/s: 0 failures, p95 8.5 s (bcrypt CPU bound, native).
- Submission storm: 54 req/s, 0 failures, 0 duplicate solves.
- Instance burst with runner outage: 476 cycles, all reached running.
- Fault injection (Redis, worker, web SIGKILL, MariaDB, runner): 0.12 % failures, all within about 1 s of a fault.
- Security: authorization sweep over all routes, 22/22 live probes, Bandit triaged, pip-audit (runner upgraded; inherited CTFd pins documented).
**Defects found and fixed**: worker memory leak (debug mode via `.flaskenv` + a single long-lived app context; found in the 30-min soaks); `ARENA_*` env vars ignored (secret key fallback); per-IP rate limits behind NAT; released instances holding quota; Redis without persistence (mass logout); nginx/gunicorn keep-alive race (502 storms); runner store column allow-list. **Not done**: ZAP scan and nmap (tools unavailable in the sandbox), third-party pentest, load tests on dedicated or larger hardware.

## Phase 17 — Documentation
docs/README.md indexes the full set. API routes and DB schema are generated from code by `tests/arena/tools/gen_reference_docs.py`.

## Final

Final regression on the tree of commit `2b6e16f3` (frozen snapshot), run on 2026-11-11:

| Suite | Result |
|---|---|
| Arena on MariaDB 10.11 + Redis | **465 passed**, 0 failed |
| Arena on SQLite | **446 passed**, 19 skipped (MariaDB-only concurrency/migration tests), 0 failed |
| Native CTFd suite with Arena force-loaded | **737 passed, 5 skipped, 2 failed**: the two `test_plugin_utils` menu-position assertions explained in TESTING_GUIDE.md (Arena's menu items are registered first). Upstream baseline without Arena: 742 passed, 2 skipped |
| Runner unit | **28 passed** |
| Runner ↔ real Docker daemon (hardening, firewall) | **6 passed** |
| Browser e2e (Playwright, desktop + mobile) | **17/17 checks passed**, no console errors |
| Live security probes against Profile C | **22/22 passed** |
| Deployment tests | included in the Arena counts above (compose validation, backup/restore round trip on MariaDB) |
| Lint (`ruff check` + `ruff format --check`) | clean |

No test was skipped, deselected or weakened to make it pass; the 19 SQLite skips are MariaDB-only tests that pass in the MariaDB run.

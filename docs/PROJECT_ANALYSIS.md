# Project Analysis — Custom CTFd Multi-Event Platform

> Phase 0 deliverable. No application code has been modified.
> Everything below was verified against the checked-out source; file references are `path:line`.

## 1. Repository identity

| Item | Value | Evidence |
|---|---|---|
| Upstream | https://github.com/CTFd/CTFd | `git clone` |
| Branch | `master` | `git branch --show-current` |
| Commit | `485d59e7bff790424f093be80ea9f6031b631f2d` (2026-11-10, "Make ratelimits configurable (#3090)") | `git log -1` |
| Latest tag | `3.8.9` (`git describe` → `3.8.6-34-g485d59e7`, tags 3.8.7–3.8.9 exist) | `git tag` |
| Declared version | `__version__ = "3.8.9"`, `__channel__ = "oss"` | `CTFd/__init__.py:36-37` |
| License | Apache License 2.0 | `LICENSE` |
| Python | `requires-python = ">=3.11,<3.12"` | `pyproject.toml:7` |
| Core stack | Flask 2.1.3, Flask-SQLAlchemy 2.5.1 / SQLAlchemy 1.4.54, Flask-RESTX 1.3.0, marshmallow 2.20.2, Flask-Caching 2.3.1, redis 4.5.5, gunicorn 23.0.0 + gevent 25.5.1, alembic 1.16.4 / Flask-Migrate 2.5.3, Pillow 13.0.0, tenacity 9.1.2, requests 2.34.2 | `requirements.txt` |
| Recommended DB | MariaDB 10.11 (compose), MySQL via `mysql+pymysql`; SQLite fallback for dev/tests | `docker-compose.yml`, `CTFd/config.py:109-123` |
| Background jobs | **None.** No Celery/RQ/APScheduler in dependencies | `grep` of `requirements.txt`/`pyproject.toml` |

Apache-2.0 permits modification and redistribution; modified files must carry change notices and the `LICENSE`/`NOTICE` must be retained.

## 2. What CTFd already provides (reusable as-is)

| Requirement area | Native capability | Location |
|---|---|---|
| Accounts, login/logout, registration, email confirmation, password reset, OAuth (MLC) | Complete | `CTFd/auth.py` (routes at 35, 117, 234, 440, 516, 545, 676) |
| Session security | Server-side sessions in cache, session invalidated on password change (`session["hash"]` vs HMAC of password hash) | `CTFd/utils/sessions/__init__.py`, `CTFd/utils/user/__init__.py:18-34` |
| CSRF | Nonce in session, checked on non-GET via `CSRF-Token` header or form `nonce` | `CTFd/utils/initialization/__init__.py:392-428` |
| API tokens | `Tokens`/`UserTokens` | `CTFd/models/__init__.py:1046-1071`, `CTFd/api/v1/tokens.py` |
| Rate limiting | `ratelimit`, `ratelimit_from_config`, per-endpoint config keys | `CTFd/utils/decorators/__init__.py:162-215`, `CTFd/config.py:196-216` |
| Challenge types (plugin registry) | `BaseChallenge` with `create/read/update/delete/attempt/solve/fail/partial/ratelimited`; registry `CHALLENGE_CLASSES` | `CTFd/plugins/challenges/__init__.py:59-372` |
| Flag validators | `static` (case-sensitive/insensitive), `regex`; registry `FLAG_CLASSES` | `CTFd/plugins/flags/__init__.py` |
| Multi-flag logic | `any`, `all`, `team` | `CTFd/plugins/challenges/logic.py` |
| Dynamic scoring | `linear`/`logarithmic` decay | `CTFd/plugins/challenges/decay.py`, `CTFd/plugins/dynamic_challenges/` |
| Hints, unlock costs, solutions, files, tags, topics, prerequisites (`requirements` JSON), `max_attempts`, scheduled release (`scheduled_at`), ratings | Complete | `CTFd/models/__init__.py:110-400`, `CTFd/api/v1/*.py` |
| Attempt protection | Redis `SETNX` per-account/challenge lock, Redis INCR KPM counter, lockout/timeout modes | `CTFd/api/v1/challenges.py:743-800` |
| Duplicate-solve protection | DB unique constraints `(challenge_id,user_id)` and `(challenge_id,team_id)` on `solves`, `IntegrityError` → `ChallengeSolveException` | `CTFd/models/__init__.py:933-962`, `CTFd/plugins/challenges/__init__.py:294-306` |
| Teams | Create/join/invite codes/captain/size limit/ban/hide | `CTFd/teams.py`, `CTFd/models/__init__.py:626-878` |
| Scoreboard | Cached standings with tie-break by earliest last solve, freeze, brackets | `CTFd/utils/scores/__init__.py`, `CTFd/api/v1/scoreboard.py:27` |
| Content gating by audience | `Modules` → `ModuleAudienceAccess` → `Audiences` → `AudienceMembers`; enforced by `can_access_challenge()` | `CTFd/models/__init__.py:1183-1262`, `CTFd/utils/modules/__init__.py` |
| Notifications / live events | SSE `/events`, `RedisEventManager` (Redis pub/sub) | `CTFd/events/__init__.py`, `CTFd/utils/events/__init__.py` |
| File storage | `FilesystemUploader` / `S3Uploader` | `CTFd/utils/uploads/uploaders.py` |
| Import/export, CSV | Complete | `CTFd/utils/exports/`, `CTFd/utils/csv/` |
| Health check | `/healthcheck` (DB exists + cache round-trip) | `CTFd/views.py:570`, `CTFd/utils/health/__init__.py` |
| Admin panel | Full single-competition admin | `CTFd/admin/`, `CTFd/themes/admin/` |
| Plugin & theme system | `load(app)`, asset registration, template override, menu bars, plugin Alembic migrations | `CTFd/plugins/__init__.py`, `CTFd/plugins/migrations.py` |

## 3. Gap analysis — what CTFd cannot do today

CTFd is architecturally **single-competition**. These facts drive the design:

1. **Global competition config.** Start/end/freeze/pause, `user_mode` (users vs teams), `team_size`, visibility — all are single rows in `config` read via `get_config()` (`CTFd/utils/__init__.py:65`). `ctftime()` (`CTFd/utils/dates/__init__.py:9`) is global.
2. **One team per user, globally.** `Users.team_id` is a single FK (`CTFd/models/__init__.py:427`); joining a team is refused if `user.team_id` is set (`CTFd/teams.py:~133`). A user cannot be on different teams in different events with native teams.
3. **`account_id` depends on the global `user_mode`.** `Users.account_id` hybrid (`CTFd/models/__init__.py:452-460`).
4. **Native score/solve views are global and unfiltered.** `Users.get_solves()`, `get_score()` (`CTFd/models/__init__.py:535-597`) and the public `GET /api/v1/users/<id>/solves` (`CTFd/api/v1/users.py:434-448`) return **every** native `Solves` row with no module/event filter. `get_standings()` sums all `Solves` (`CTFd/utils/scores/__init__.py:10-140`).
   **Consequence:** if event solves were written into native `solves`, they would leak across events through public profile APIs and inflate global scores.
5. **Dynamic decay counts native `Solves` globally** (`CTFd/plugins/challenges/decay.py:9-21`).
6. **No background worker / scheduler** exists. Plugin Alembic migrations run inside `load(app)` on every process start (`CTFd/plugins/migrations.py:52`), which races when several gunicorn workers start simultaneously on MySQL.
7. **In-memory `EventManager.publish()` enqueues the message into *every* client's per-channel queue** (`CTFd/utils/events/__init__.py:43-48`). Publishing on a channel nobody drains grows queues without bound. `RedisEventManager.listen()` is only started for channel `"ctf"` (`CTFd/utils/initialization/__init__.py:198-205`).
8. **Tracking writes synchronously** in a `before_request` hook (`CTFd/utils/initialization/__init__.py:252-305`) — throttled by a 5-minute cache, but on the request path.
9. **RBAC is binary**: `Users.type in {"user","admin"}`; `admins_only` checks `is_admin()` (`CTFd/utils/decorators/__init__.py:125`).
10. **No Attack & Defense, container execution, external lab, certificate, event, or activity-analytics features.**

## 4. Key reusable lever: Modules as an isolation wall

Every native participant path that exposes challenge content calls `can_access_challenge()`:

- challenge list/detail/attempt — `CTFd/api/v1/challenges.py:193, 357, 725`
- challenge solves — `CTFd/api/v1/challenges.py:1086`
- hints — `CTFd/api/v1/hints.py:130`
- unlocks — `CTFd/api/v1/unlocks.py:153`
- solutions — `CTFd/api/v1/solutions.py:176`
- file downloads — `CTFd/views.py:~438`

`can_access_challenge()` returns `False` for a challenge whose `module_id` is set to a module **with no audience** (`CTFd/utils/modules/__init__.py:35-50`). Placing each event's challenges into an *event-sealed module* hides them from every native participant path **without modifying core code**, while keeping them editable in the native admin challenge editor (flags, hints, files, tags all reused).

## 5. Decision summary (needs your approval)

| # | Decision | Recommendation | Rationale |
|---|---|---|---|
| D1 | Where event submissions are recorded | **Plugin-owned event tables** (`arena_solves`, `arena_submissions`), *not* native `solves` | Avoids the leakage in §3.4, the global unique constraints, and global decay. Native challenge **type classes are reused for `attempt()` (flag validation)** so static/regex/custom flags and custom types keep working. |
| D2 | Event teams | **Plugin-owned `arena_teams` + `arena_team_members` (unique per event+user)** | Native `Users.team_id` is single-valued (§3.2); changing it is a wide core change. Native teams keep working for the legacy global CTF. |
| D3 | Event challenge isolation | **Event-sealed native Module** per event + `arena_event_challenges` (unique `challenge_id`) | Zero-core-change isolation (§4). |
| D4 | Background jobs | **One worker process** (`flask arena worker`) using DB-backed job table + Redis leader lock; no Celery | Smallest dependency footprint; needed for A&D rounds, cleanup, PDFs, analytics. |
| D5 | Live scoreboard transport | **Cached polling with ETag/304** (15 s), optional SSE "changed" ping on native `ctf` channel only | Avoids §3.7 queue growth; 100 users ≈ 7 req/s on a Redis-cached endpoint. |
| D6 | Container execution | **Separate `runner/` service on an isolated host**, HMAC-signed API; the CTFd container never sees a Docker socket | Security boundary required by the brief. |
| D7 | PDF certificates | **Pillow** (already a dependency) renders template image + text → PDF in the worker | No new heavy dependency (WeasyPrint needs system libs). |
| D8 | Plugin layout | **One plugin package `CTFd/plugins/arena/`** with internal sub-modules | CTFd loads plugins in alphabetical order (`get_plugin_names()` sorts); one package controls internal init order and has one migration chain. |
| D9 | Core modifications | Target **zero**; at most two small, isolated patches if load testing proves them necessary (see `CUSTOM_ARCHITECTURE.md §9`) | Backward compatibility. |

## 6. Risks identified up front

| Risk | Impact | Mitigation |
|---|---|---|
| Custom challenge plugins that put side effects in `solve()` rather than `attempt()` | Those side effects won't run for event submissions (D1) | Document; provide `ArenaChallengeHooks` opt-in interface; built-in `standard`/`dynamic` fully supported |
| Admin edits an event challenge's `module_id` in native UI | Could unseal a challenge into the global CTF | Integrity job + `before_request` admin-side validation in the plugin admin API; nightly integrity check reports drift |
| Plugin migration race on multi-worker start | Duplicate DDL errors | Run migrations in a dedicated `migrate` compose job; `load()` skips if `ARENA_MIGRATE_ON_LOAD=false` |
| A&D checker logic must reach vulnerable team services | Lateral movement into platform | Checkers run on the isolated runner; results returned via signed API only |
| SQLite in tests vs MariaDB in prod | Constraint/locking differences | Integration tests also run against MariaDB in CI (compose) |

## 7. Baseline test status

See `TESTING_PLAN.md §2` for the recorded baseline run of the unmodified upstream suite.

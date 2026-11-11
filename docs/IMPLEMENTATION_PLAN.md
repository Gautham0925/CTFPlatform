# Implementation Plan — phased

Each phase is independently testable and mergeable, and leaves native CTFd fully working. Every phase follows the brief's 10-step workflow: inspect → document → smallest change → tests → existing tests → compat check → docs → report.
**A phase is "done" only when its exit criteria pass. Partial work is reported as partial.**

Ordering follows your recommendation: **multi-event isolation before any frontend**, and A&D/containers isolated.

| Phase | Scope | Depends on | Exit criteria |
|---|---|---|---|
| **0** | Repository analysis + these 8 docs | — | ✅ Docs delivered; baseline tests recorded; **architecture approved by you** |
| **1. Foundation** | `arena` plugin skeleton, `load()`, settings, Alembic chain (rev 1: `arena_events`, `arena_registrations`, `arena_roles`, `arena_jobs`), event-context resolver, authz guards, RBAC, `flask arena migrate / db-downgrade / check-integrity`, migrations off web workers | 0 | All 735 native tests still pass; migration up/down/up on MariaDB copy; guard unit tests (IDOR matrix) |
| **2. Events + registration** | Event CRUD (admin API + minimal admin page), lifecycle state machine, registration (self/admin/approval/limits/windows), sealed-module creation | 1 | Two events run concurrently; a user registered in A gets 404 on every B endpoint; cap enforcement under 50 concurrent registrations (no over-admission) |
| **3. Event teams** | Teams, invites (signed, expiring), join/leave, captain transfer, size limits, ban/remove, admin enrollment | 2 | UNIQUE(event,user) holds under concurrent joins; team ops event-scoped; native team tests untouched |
| **4. Jeopardy engine** | Challenge assignment/cloning, sealing, event challenge list/detail, attempt endpoint reusing `chal_class.attempt`, event decay, hints, files gateway, prerequisites, attempt limits | 3 | Native types static/regex/dynamic pass in-event; **duplicate-solve race test (100 parallel correct submissions → exactly 1 solve, 1 ledger row)**; event challenges 404 on all native endpoints |
| **5. Scoring service + scoreboard** | Ledger, totals, Jeopardy derivation, freeze, tie-break, cached scoreboard API with ETag, score history, recalculation with audit | 4 | Ledger replay reproduces totals; freeze hides post-freeze for non-admins; config change creates new version, never mutates old |
| **6. Worker + jobs** | `arena_jobs`, leader lock, scheduler, graceful shutdown (SIGTERM drains), dead-letter | 1 | Kill -9 mid-job → job retried, no duplicate effects; Redis down → worker pauses, web unaffected |
| **7. Activity + analytics** | Buffered activity, flush job, retention, rollups, admin analytics pages | 6 | Submission p95 unchanged with activity on (A/B); Redis-down drops activity safely |
| **8. Runner service** | `runner/` service + hardening, HMAC auth, image registry/validation, platform client, instance lifecycle, reaper | 6 | Lifecycle test against runner in CI (docker-in-docker); escape checks: no host net, no metadata, no DB/Redis reachability, caps dropped |
| **9. Attack & Defense** | Services, team service provisioning, rounds, flag gen/plant, capture API, checker result ingestion, round close scoring, penalties, audit views | 5, 6, 8 | Deterministic replay of a 10-round simulated game gives identical totals; forged/replayed runner messages rejected |
| **10. Mixed scoring** | Weighted combination, per-component boards, unified ranking | 5, 9 | Golden-file tests of mixed formula; tie-break rules |
| **11. External labs** | Adapter contract, `MockProvider`, `GenericHTTPProvider`, sessions, polling verified score events, admin test button | 6 | Contract test suite passes for Mock; SSRF tests; timeouts/retries verified |
| **12. Certificates** | Templates, eligibility, async PDF (Pillow), download, verify page, revoke | 5, 6 | Ineligible user cannot be issued; verify shows revoked; PDF generated off request path |
| **13. Participant dashboard** | Event list, countdowns, announcements, challenges, team, rank, submissions, A&D status, lab info, certificates; mobile | 4–12 | Lighthouse perf ≥ 90 mobile on dashboard; no new JS framework |
| **14. Admin dashboard** | Overview, health (DB/Redis/worker heartbeat/runner), all management pages, RBAC-aware | 4–12 | RBAC matrix tests |
| **15. Deployment** | Production compose (nginx TLS, internal nets, no published DB/Redis), `.env.example`, backup/restore scripts, log rotation, runner host guide | 8 | Fresh deploy from docs on clean VM; restore drill |
| **16. Load & security validation** | Locust suite, 60-min soak + spikes, security review report | all | Targets in `PERFORMANCE_PLAN.md` met **with measured numbers**; findings fixed or documented |
| **17. Docs finalization** | README, ARCHITECTURE, DATABASE_SCHEMA, API_DOCUMENTATION, PLUGIN_DOCUMENTATION, DEPLOYMENT, SECURITY, PERFORMANCE, TESTING, MIGRATION_GUIDE, CHANGELOG | all | Docs match code |

## Code layout (Phase 1 creates it)

```
CTFd/plugins/arena/
  __init__.py            load(app): config → models → blueprints/API ns → admin menu → CLI
  config.py  models/  core/(context.py, authz.py, rbac.py)  events/  teams/  jeopardy/
  scoring/  ad/  execution/  labs/  activity/  analytics/  certificates/  jobs/  ui/
  templates/  assets/  migrations/
runner/                  # separate deployable, own Dockerfile & requirements
deploy/                  # production compose, nginx, scripts
tests/arena/             # mirrors plugin layout; uses tests/helpers.py
loadtest/                # locustfile.py, scenarios, reports/
```

## Branching
Work continues on the local branch `arena/phase-0-analysis`, then one branch per phase. Nothing is pushed: this clone points at upstream `CTFd/CTFd`. **Tell me which remote (your fork) to push to.**

## Open questions for you
1. Approve D1–D9 in `PROJECT_ANALYSIS.md §5`, in particular **D1/D2 (plugin-owned event submissions and teams instead of native `solves`/`teams`)**.
2. Will the runner host be a separate VM (recommended), or must the first iteration run on one machine (in which case it needs a separate Docker daemon or rootless Docker plus gVisor `runsc` as the minimum)?
3. Should the legacy native single-CTF mode stay available to participants alongside events, or only to admins?
4. Is there a specific external lab provider to target first, beyond the mock?

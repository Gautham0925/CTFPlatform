# Arena: final report (Phases 0–17)

CTFd 3.8.9 (commit `485d59e7`) extended by the **Arena** plugin into a
self-hostable multi-event CTF platform. Branch `arena/phases-2-17`. No CTFd
application code was modified; the only native-tree change is one
`.gitignore` line. Every number below comes from an actual run;
IMPLEMENTATION_STATUS.md, PERFORMANCE_REPORT.md and SECURITY_GUIDE.md have
the details.

## 1. Overall architecture

```
             participants / staff (browser)
                       │ HTTPS
                     nginx  (TLS 1.2/1.3, HSTS, per-IP flood limits, upstream failover)
                       │
     CTFd web × N (gunicorn/gevent) ── CTFd core (users, auth, sessions, native CTF)
          └─ Arena plugin: /arena pages, /api/v1/arena/*, guards, RBAC, services
                       │                       │
                 MariaDB 10.11            Redis 7 (sessions, cache, locks, buffers)
                 (source of truth)
                       │
       Arena worker × N (`flask arena worker`): DB job queue, leader lease, periodic jobs
                       │ HTTPS + HMAC (private CA)
                 Runner host (separate machine): runner API ── Docker ── challenge / checker containers
                       │ HTTPS (SSRF-checked)
                 External lab providers (generic REST contract)
```

Principles: extend, don't fork (plugin + Alembic revisions; native code
untouched); the **event comes only from the URL** and every object is scoped
to it; event content sits in a **sealed native module**, so native CTFd can
never show it; **the database is the source of truth**, with correctness
enforced by UNIQUE constraints and compare-and-set updates; Redis is a cache
for Arena (CTFd itself needs it for sessions); the platform **never** touches
Docker.

## 2. All implemented phases

| Phase | Result |
|---|---|
| 0 Analysis | PROJECT_ANALYSIS, EXISTING/CUSTOM_ARCHITECTURE, DATABASE_DESIGN, plans |
| 1 Foundation | plugin skeleton, RBAC, guards, schema guard, health, CLI, rev 0001 |
| 2 Events & registration | lifecycle, visibility, windows, capacity, approval, invites |
| 3 Event teams | per-event teams, codes, invites, captaincy, locking |
| 4 Jeopardy engine | sealed challenges, flags, hints, files, prerequisites, exactly-once solves |
| 5 Scoring & scoreboard | ledger, dynamic values, first blood, freeze, ETag cache, verify |
| 6 Worker & jobs | DB queue, leases, retries, dead letter, leader lease, periodic jobs |
| 7 Activity & analytics | privacy-preserving activity, rollups, reports, CSV |
| 8 Runner | standalone HMAC/TLS service, hardened Docker backend, firewall |
| 9 Attack & Defense | services, rounds, flags, captures, checkers, SLA scoring |
| 10 Mixed scoring | versioned weights/tie-break with snapshots |
| 11 External labs | provider interface, mock + generic HTTP, encrypted tokens |
| 12 Certificates | optional, **off by default**, PDF + public verification |
| 13–14 Dashboards | participant pages, admin console, platform page |
| 15 Deployment | Profiles A/B/C, scripts, systemd; all verified live |
| 16 Validation | load, soak, fault injection, security sweep/probes; 11 defects fixed |
| 17 Documentation | full set, indexed in docs/README.md |

## 3. Features completed

Multi-event competitions (Jeopardy, A&D, mixed); public, unlisted and private
events; registration with capacity, approval, email-domain rules and
invitation codes; per-event teams; static, regex, case-insensitive and
per-team flags; dynamic scoring; hints with costs; files; prerequisites;
release windows; first blood; score freeze; adjustments; scoreboard history
and categories; container challenges per team (runner); Attack & Defense
with checkers; external labs; announcements; activity analytics and CSV
exports; audit log; optional certificates; per-event staff roles;
participant and admin dashboards; worker with retries and a dead-letter
queue; health and readiness endpoints; backup and restore; three deployment
profiles.

## 4. Database schema summary

35 `arena_*` tables in 13 reversible Alembic revisions (`a7e1f00d0001`–`0013`);
none of CTFd's own tables are altered. Key invariants: `uq_arena_solve_once`,
`uq_arena_members_event_user`, `uq_arena_score_source` (ledger once per
source), `active_key` uniques for instances and lab sessions,
`uq_arena_jobs_dedupe`, CHECK constraints for every status column.
Generated per-table reference: DATABASE_SCHEMA.md.

## 5. API summary

153 route/method pairs (generated table in API_DOCUMENTATION.md), grouped as:
public event data (`/events`, by-slug, scoreboards, announcements);
participant actions (register, teams, challenges, attempt, hints, instances,
labs, A&D flags, my-score); admin per event (`/admin/events/<id>/…`:
settings, registrations, teams, challenges, submissions, scoring, A&D, labs,
certificates, announcements, audit, exports, roles); platform
(`/admin/platform/…`: images, runner health, providers, jobs, workers).
Conventions: JSON envelope with a stable error `code`, CSRF header, 404 for
invisible objects, pagination, weak ETags. The runner API and the external
lab provider contract are documented there too.

## 6. RBAC summary

The superadmin is CTFd's admin and is the only one who can create events,
manage roles and manage the platform. Per-event roles: **organizer** (all
event permissions), **challenge_manager**, **scoreboard_operator**,
**ad_operator**, **cert_manager**, **analyst**. They map onto 14 permissions
(`event.view_admin`, `event.manage`, `registrations.manage`, …). Guards check
the permission for the event in the URL. `test_authz_matrix.py` sweeps every
route as an anonymous visitor and as a non-participant.

## 7. Participant dashboard overview

`/arena` (competition list), `/arena/join` (invitation code), and
`/arena/e/<slug>/` with tabs **Overview** (description, rules,
announcements, countdown), **Challenges** (categories, detail modal, hints,
files, instance start/stop, submission), **Scoreboard** (standings with
breakdown, SVG history, ETag polling), **Team**, **A&D** (A&D events),
**Labs**, and **My progress** (solves, score history, certificates). Tabs
follow the event mode. Pages are server-rendered shells with plain JS and no
build step. Playwright e2e covers desktop and mobile.

## 8. Admin dashboard overview

`/arena/admin`: the competitions you can manage, plus create (superadmin).
`/arena/admin/e/<id>`: 13 permission-filtered tabs — Overview, Settings,
Registrations, Teams, Challenges, Submissions, Scoreboard (freeze,
adjustments, verify, scoring versions), Attack & Defense, Labs & instances,
Certificates, Announcements, Audit log, Exports. `/arena/admin/platform`
(superadmin): system health, workers, dead jobs, challenge images (digest
validation), lab providers.

## 9. Runner architecture

A separate service (`runner/`, Flask + gunicorn) on a **separate host**, and
the only component with the Docker socket. Platform → runner traffic uses
HTTPS verified by a private CA, HMAC-SHA256 over method, path, timestamp,
nonce and body hash, a 60 s skew window, and a nonce replay store. Images
must be on a digest allow-list. Containers run with no capabilities,
no-new-privileges, a read-only root, noexec tmpfs, memory/CPU/pids limits, a
private bridge per instance and ports on a chosen IP; optional forced
non-root user and gVisor. A firewall script blocks the host, private ranges
and metadata. A reaper handles TTLs and orphans. The platform drives it
through the job queue (provision, destroy, sync, expire). Details:
RUNNER_SECURITY.md.

## 10. Attack & Defense implementation

Services per event; a team-service matrix; rounds advanced by the worker
(`ad-tick`); flags derived per team, service and round with HMAC
(`ARENA_FLAG_SECRET`) and a lifetime in rounds; submission rejects own,
expired and duplicate flags, with a per-team rate limit; captures, SLA
(checker status) and defense points go into the same ledger; checkers run
as containers on the runner (`/v1/ad/check`, isolated network, timeout) and
report up, down, mumble or corrupt; scoreboard and status endpoints.
Combined with Jeopardy through mixed weights.

## 11. External lab integration

`LabProvider` interface with `MockProvider` and `GenericHTTPProvider`
(REST contract: health, labs, sessions create/status/connection/reset/delete).
https only, a public-address check at configuration and on every request, no
redirects, timeouts, retries for idempotent calls, a 1 MB response cap,
field sanitising, Fernet-encrypted tokens. Sessions have quotas and
`active_key` uniqueness, and the worker syncs, expires and health-checks
them. Connection details go to the owner only.

## 12. Optional certificate implementation

Off unless `ARENA_CERTIFICATES_ENABLED=true`; when off, its routes are not
registered at all. Templates (participation and winner rules), issuing as a
worker job, PDF rendering with Pillow and a bundled open font, a public
verification page that requires the printed code (HMAC with
`ARENA_SECRET_KEY`), and revocation.

## 13. Deployment instructions

Profiles: **A** `deploy/compose/dev.yml` (local), **B**
`deploy/compose/community.yml` (single host, TLS), **C** B +
`competition.yml` + `runner.yml` on a second host. Full steps:
deploy/README.md; bare metal: INSTALLATION_GUIDE.md. Exact commands are at
the end of this report.

## 14. Test results

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

Load, soak and fault-injection results are in §16.

## 15. Security assessment

Controls and evidence: SECURITY_GUIDE.md. Phase 16 testing: a dynamic
authorization sweep over all 153 route/method pairs (pass); **22/22** live
black-box probes against Profile C (TLS versions, headers, CSRF,
content-type, IDOR across events, native leakage, injection, XSS
sanitizing, traversal, oversized bodies, runner signature); Bandit with
0 high findings (the rest triaged); pip-audit, after which the runner was
upgraded. Defects found and fixed: ignored `ARENA_*` env (crypto key
fallback), per-IP rate limits behind NAT, instance-quota lockout, runner CA
key in the platform mount, readiness probe before setup, non-persistent
Redis sessions, worker debug-mode memory leak. **Not done:** ZAP and nmap
(not available in this sandbox) and a third-party penetration test.
Assessment: no known unmitigated high-severity issue in Arena's own code.
The inherited CTFd dependency CVEs (Flask 2.1.3, Werkzeug 2.2.3, …) are the
main residual risk and need an upstream CTFd upgrade.

## 16. Performance measurements

On one 4-vCPU / 15 GB VM that also ran the load generator and the runner
(PERFORMANCE_REPORT.md):

| Scenario | Requests | Failures | Key latency |
|---|---|---|---|
| 100 players, 10 min | 15,183 | 0 | API p95 ≤ 42 ms |
| Soak 30 min (read-heavy) | 38,920 | 0 | p95 30 ms |
| Soak 30 min (6,133 submissions) | 44,841 | 0 | p95 35 ms |
| After worker fix, 15 min | 22,562 | 0 | p95 41 ms; worker memory flat |
| Submission storm | 9,701 | 0 | 54 req/s, p95 82 ms |
| 150 logins at 50/s | 2,805 | 0 | login p95 8.5 s (bcrypt CPU) |
| Instance burst + runner outage | 476 cycles | 0 | time to running p50 5.1 s |
| Fault injection | 16,144 | 20 (0.12 %) | all within about 1 s of a fault |

The 100-concurrent-user target was **met on this hardware**; nothing beyond
it is claimed.

## 17. Known limitations

- One team per user per event (a deliberate integrity guarantee).
- Logins are CPU-bound (native bcrypt): about 14 logins/s on 4 vCPUs.
- A Redis outage causes native CTFd 500s (sessions) for its duration.
- Load figures come from a single shared VM. Container-challenge scale was
  tested with only 30 concurrent users.
- No MFA (native CTFd). The superadmin is CTFd's admin account.
- In the sandbox, the image was built without the Dockerfile's `apt-get`
  steps (egress restrictions). Those lines are untested here.
- ZAP, nmap and an external penetration test were not run.
- 2 native tests fail only in the forced-plugin run, because of menu-list
  position (TESTING_GUIDE.md).

## 18. Remaining technical debt

- Upgrade to a CTFd release with current Flask and Werkzeug, then re-run the
  native and Arena suites.
- The lab-provider DNS-rebinding window: pin the resolved IP in the HTTP
  adapter.
- Record a 429 reason breakdown in the load suite (partly done).
- The admin UI is functional but basic: no drag-and-drop ordering and no
  bulk challenge import.
- `test_migrations` and the concurrency tests need a MariaDB service in CI.
  Add a CI workflow (none is committed).
- The test database is created with `utf8mb4_general_ci` while production
  uses `unicode_ci`. Run the suite against a `unicode_ci` database in CI as
  well.
- Phase 0 planning documents are kept as written; where they disagree with
  the code, the guides are authoritative.

## 19. Backup and recovery instructions

Backups are consistent: a single-transaction `mariadb-dump` of the database,
the uploads archive, a manifest (schema revisions, git commit) and SHA-256
checksums. Restore refuses without `--yes` or on a checksum mismatch, stops
web and worker, restores, migrates, runs the integrity check and starts
again. Verified on Profile B: after a destructive change, the restore gave
identical table checksums and upload hashes. Redis is not backed up; players
log in again after a restore. Keep `deploy/.env` (especially
`ARENA_SECRET_KEY`) backed up separately and securely. Commands are below.

## 20. Production readiness assessment

**Ready for small and medium events (around 100 concurrent players on
4 vCPUs), on Profile B or C, after you:** run the go-live checklist in
deploy/README.md; build the image on your own host (the `apt-get` steps run
there); repeat the load test on your production hardware; and rehearse a
restore. For a large or high-stakes public event, first: upgrade the
inherited CTFd dependencies, get an external security test and a ZAP scan,
and size the CPU for your peak login rate. A&D and container challenges are
functionally complete and tested. Run the runner on a dedicated host, with
gVisor if challenge authors are untrusted.

---

## Exact commands

**Install dependencies** (Docker route; bare metal: INSTALLATION_GUIDE.md):

```bash
sudo apt-get install -y docker.io docker-compose-v2 git openssl
```

```bash
git clone -b arena/phases-2-17 https://github.com/Gautham0925/CTFPlatform.git && cd CTFPlatform
```

**Configure environment variables:**

```bash
cp deploy/examples/platform.env.example deploy/.env && deploy/scripts/gen-secrets.sh deploy/.env
```

Then edit `deploy/.env` to set `ARENA_DOMAIN` and `ARENA_PUBLIC_URL`, and
put your TLS certificate in `deploy/nginx/certs/fullchain.pem` +
`privkey.pem`. For a quick test only, generate a self-signed one:

```bash
deploy/scripts/selfsigned-cert.sh ctf.example.org
```

**Start the application** (Profile B; runs the migrations, web, worker,
nginx, MariaDB and Redis):

```bash
deploy/scripts/install.sh
```

or directly:

```bash
docker compose --env-file deploy/.env -f deploy/compose/community.yml up -d --build
```

Local evaluation instead (Profile A):

```bash
docker compose -f deploy/compose/dev.yml up -d --build
```

**Start background workers** (included in the stack above; to scale or run one by hand):

```bash
docker compose --env-file deploy/.env -f deploy/compose/community.yml up -d --scale worker=2 worker
```

```bash
flask arena worker
```

**Run migrations** (automatic in the `migrate` service; manually):

```bash
docker compose --env-file deploy/.env -f deploy/compose/community.yml run --rm migrate migrate
```

```bash
flask db upgrade && flask arena migrate && flask arena check-integrity
```

**Start optional runner services** (on the runner host):

```bash
cp deploy/examples/runner.env.example runner/.env && deploy/scripts/runner-tls.sh runner.internal.example
```

```bash
docker compose -f deploy/compose/runner.yml up -d --build && sudo sh runner/firewall/docker-user.sh
```

On the platform host, set `ARENA_RUNNER_URL`, `ARENA_RUNNER_KEY_ID`,
`ARENA_RUNNER_SECRET` and `ARENA_RUNNER_CA` in `deploy/.env`, copy
`runner-ca/ca.crt`, then:

```bash
docker compose --env-file deploy/.env -f deploy/compose/community.yml -f deploy/compose/competition.yml up -d
```

**Run tests** (from the repository root, dev environment via `uv sync --frozen --group dev`):

```bash
.venv/bin/python -m pytest -q -p no:randomly tests/arena
```

```bash
TESTING_DATABASE_URL='mysql+pymysql://ctfd:ctfd@localhost/ctfd' .venv/bin/python -m pytest -q -p no:randomly tests/arena
```

```bash
PYTHONPATH=tests/arena/tools .venv/bin/python -m pytest -q -n 3 -p force_plugins tests --ignore=tests/arena
```

```bash
cd runner && .venv/bin/python -m pytest -q tests/test_runner.py
```

**Create backups:**

```bash
deploy/scripts/backup.sh
```

**Restore backups:**

```bash
deploy/scripts/restore.sh deploy/backups/arena-<timestamp> --yes
```

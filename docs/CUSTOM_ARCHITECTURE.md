# Custom Architecture — "Arena" extension for CTFd

Status: **PROPOSED, awaiting approval.** Nothing here is implemented yet.

## 1. Principles

1. **Modular monolith inside CTFd.** One plugin package `CTFd/plugins/arena/`. No microservices except the container **runner**, which has to sit outside the web app's security boundary.
2. **Zero core edits by default.** Core edits are allowed only through the gate in §9.
3. **Reuse native content, own the event state.** Challenges, flags, hints, files, tags and topics stay native and editable in the native admin. Event registrations, event teams, event submissions, scores, A&D, labs, activity and certificates live in `arena_*` tables.
4. **Server-side authority.** Clients never send scores, event ownership or completion claims that the server trusts. Every event-scoped request resolves the event from the URL, then checks the caller's registration or role in the database.
5. **Append-only scoring ledger.** Scores come from stored source events and can be reproduced from them.

## 2. Component map

```
┌───────────────────────────── CTFd web (gunicorn/gevent) ─────────────────────────────┐
│ Core CTFd (unchanged): auth, sessions, CSRF, native challenges/admin, native CTF     │
│                                                                                       │
│ CTFd/plugins/arena/                                                                   │
│   core/        Event context resolver, authz guards, RBAC roles, settings             │
│   events/      Event CRUD, registration, lifecycle, archive/export                    │
│   teams/       Event teams, invites, captain, limits, bans                            │
│   jeopardy/    Event challenge assignment, event attempt API (reuses chal_class),     │
│                event decay, hint unlocks, event file gateway                          │
│   ad/          A&D services, rounds, flag generation/validation, SLA ingestion        │
│   scoring/     Ledger, materialized totals, mixed formula, freeze, tie-break, recalc  │
│   execution/   Runner client (HMAC), image registry, instance lifecycle               │
│   labs/        Provider adapter interface, mock + HTTP adapters, sessions             │
│   activity/    Non-blocking activity buffer → batched DB writes, retention            │
│   analytics/   Rollup jobs, read-only dashboards                                      │
│   certificates/ Templates, eligibility rules, issuance, revocation, verify page       │
│   jobs/        DB job queue + Redis leader lock + scheduler (used by worker)          │
│   ui/          Participant dashboard + admin pages (Jinja + small vanilla JS)         │
│   migrations/  Alembic chain for arena_* tables                                       │
└──────────────┬───────────────────────────────────────────────┬────────────────────────┘
               │ same codebase, `flask arena worker`           │ HTTPS + HMAC (runner API)
        ┌──────▼──────┐                                  ┌─────▼────────────────────────┐
        │ arena-worker│  rounds, expiry, PDFs, rollups,  │ runner/ (ISOLATED HOST)       │
        │ (1 replica) │  activity flush, lab polling     │ Docker socket lives ONLY here │
        └─────────────┘                                  │ challenge + A&D containers,   │
                                                         │ checkers, egress firewall     │
                                                         └───────────────────────────────┘
```

## 3. Event isolation model (decisions D1–D3)

### 3.1 Event context
- Every participant and admin URL is event-scoped: `/arena/e/<slug>/...` and `/api/v1/arena/events/<event_id>/...`.
- `resolve_event(event_id)` loads the event (404 if missing or not visible to the caller). Then `require_participant(event)` or `require_event_role(event, role)` checks `arena_registrations` / `arena_roles` in the DB. Event ids in a body or query string are ignored for authorization. Any child object (challenge, team, instance, lab session, certificate) is reloaded with `event_id == resolved.id` in the WHERE clause. That rule is the main IDOR control.

### 3.2 Challenges
- Each event gets a native **sealed Module**: `modules` row plus `arena_events.sealed_module_id`, with no audience. Event challenges get `module_id = sealed_module_id`. Native participant endpoints then return 404 for them (`PROJECT_ANALYSIS.md §4`).
- `arena_event_challenges(event_id, challenge_id UNIQUE, …)` adds event-level data (points override, difficulty, visibility window, attempt limit override, category order).
- Reusing a challenge in another event means **cloning it**: the native rows (challenge + flags + hints + files + tags) are copied. One challenge row belongs to exactly one event, which makes the uniqueness rules simple.

### 3.3 Teams
- `arena_teams` (per event) and `arena_team_members` with **UNIQUE(event_id, user_id)**: one team per user per event, any number of events per user. Invite codes are signed with `itsdangerous` (native `CTFd.utils.security.signing`), scoped to event and team, and expire.
- Individual events have no team. The scoring entity is `(event_id, "user", user_id)`.

### 3.4 Scoring entity
`entity_key = "t:<team_id>"` or `"u:<user_id>"`, always together with `event_id`. Every score and solve row carries both.

## 4. Jeopardy engine

`POST /api/v1/arena/events/<eid>/challenges/<cid>/attempt`

1. Resolve event and participant. The event must be running and not paused.
2. Load the `arena_event_challenges` row `WHERE event_id=eid AND challenge_id=cid`; check visibility, schedule and prerequisites, all event-scoped.
3. Rate limit and attempt limit through the native-style Redis `SETNX` lock and `INCR`, with keys prefixed `arena:{eid}:{entity}:{cid}`.
4. `chal_class = get_chal_class(challenge.type)`, then `chal_class.attempt(challenge, request)`. This **reuses native flag validation**: static, regex, custom flag classes, `any/all/team` logic, and custom challenge types.
5. One DB transaction:
   - always `INSERT arena_submissions` (status, provided text hashed+truncated for wrong answers, ip)
   - on correct: `INSERT arena_solves` with **UNIQUE(event_id, challenge_id, entity_key)**. On `IntegrityError` the response is `already_solved` and no points are awarded. This is the hard guarantee against duplicate scoring under races.
   - on correct: `INSERT arena_score_ledger(source='jeopardy_solve', source_ref='solve:<id>', UNIQUE(event_id, source, source_ref))`.
6. After commit: invalidate the event scoreboard cache key, then push activity to the buffer. Neither step can fail the request.

**Dynamic scoring:** `scoring/decay.py` reuses the native `DECAY_FUNCTIONS` formulas, but the solve count is event-scoped (it counts `arena_solves`, not native `Solves`). As in native CTFd, the value of a dynamic challenge changes for everyone who solved it. Jeopardy totals are therefore *derived* (sum of current values over solves), not stored per solve. The ledger records the solve with `points=NULL, kind='derived'`, which keeps the audit trail without silently freezing a value.

**Hints:** event hint unlock endpoint → `arena_hint_unlocks` + ledger row `hint_cost` (negative).
**Files:** `/arena/e/<slug>/files/<file_id>` checks event participation, then streams through the native uploader (`get_uploader()`).

## 5. Attack & Defense engine (independent)

Entities: `arena_ad_services` (service definition: image ref, ports, checker ref), `arena_ad_team_services` (per-team instance on the runner), `arena_ad_rounds` (tick schedule), `arena_ad_flags` (generated per round/team/service), `arena_ad_flag_captures`, `arena_ad_checks` (SLA results), `arena_ad_penalties`.

- **Flags** are generated by the platform: `FLAG{base32(HMAC(event_secret, round|team|service|nonce))}`. They are stored hashed and planted by the runner's checker over the signed API.
- **Attack submission** (`POST …/ad/flags`): one or more flags. Each is validated server-side: it exists, it is not expired (`valid_for_rounds`), it does not belong to the submitting team, and it has not been captured before (UNIQUE(event_id, flag_id, attacker_team_id)).
- **Defense/SLA**: the runner runs checkers each round and POSTs signed results (`up/mumble/corrupt/down`). The platform accepts a result only for the current or previous round and the expected team/service, and stores it idempotently (UNIQUE(round_id, team_id, service_id)).
- **Round transitions**: the worker's scheduler holds a Redis leader lock and advances rounds when they are due. Closing a round writes ledger rows for that round in a single transaction (`ad_attack`, `ad_defense_loss`, `ad_sla`) under `source_ref='round:<id>:…'`, so a re-run is a no-op.
- **Scoring formula** is a versioned config (`arena_scoring_configs`). Factor weights cover SLA, attack, defense, participation and penalties. Every ledger row records the `config_version` it was computed with.

## 6. Unified scoring

- `arena_score_ledger` is append-only and covers every non-derived component: A&D rows, bonus, penalty, hint costs.
- `arena_score_totals(event_id, entity_key, component)` holds materialized sums, updated in the **same transaction** as the ledger insert (`UPDATE … SET points = points + :delta`).
- The Jeopardy component is derived on demand by one indexed grouped query per event, cached in Redis until the next solve.
- **Mixed ranking** = `w_j·J + w_ad·AD + bonus − penalty`. Weights come from the active `arena_scoring_configs` version. Tie-breaks are configurable: earliest time to reach the score (default, same as native), fewest wrong submissions, or most A&D captures.
- **Freeze**: `events.freeze_at`. Public views filter ledger and solves by `created_at < freeze_at`; admins see live data.
- **Config changes never rewrite history.** A new config creates version N+1. Recalculation is an explicit admin action. It writes an `arena_recalc_runs` record with before/after totals and actor, and rebuilds totals from the ledger. The ledger is never edited; corrections are compensating rows.

## 7. Docker challenge execution

- The `runner/` service is a small Python service, separate from CTFd, deployed on an **isolated host**. It owns the Docker socket. Its API is authenticated with HMAC-SHA256 (`X-Arena-Key-Id`, `X-Arena-Timestamp`, `X-Arena-Signature`), with ±60 s skew and a nonce replay cache. mTLS is optional.
- **Platform side** (`execution/`): image registry (`arena_images`: name, **pinned digest**, version, limits, validated flag), `arena_instances` (owner entity, event, challenge, expiry, status, endpoint). Every create/restart/stop is idempotent through `Idempotency-Key = instance uuid`.
- **Runner hardening defaults**: `--cap-drop=ALL`, `no-new-privileges`, non-privileged, `--pids-limit`, `--memory`, `--cpus`, `--read-only` + tmpfs, userns-remap, one internal bridge per instance, egress denied by a `DOCKER-USER` iptables chain (RFC1918, link-local and metadata IPs blocked), only digest-pinned images from the allowlist, and a TTL reaper on the runner that does not depend on the platform.
- Endpoints are `host:port` on the runner's public interface, allocated from a port range. HTTP challenges can optionally get a per-instance random subdomain.

## 8. External labs

`labs/base.py` defines the adapter contract:

```python
class LabProvider(Protocol):
    def health(self) -> ProviderHealth
    def create(self, req: LabRequest, idem_key: str) -> LabHandle
    def start(self, h) / stop(self, h) / reset(self, h) / destroy(self, h)
    def connection_info(self, h) -> ConnectionInfo
    def status(self, h) -> LabStatus            # includes completion
    def fetch_score_events(self, h, since) -> list[VerifiedScoreEvent]   # provider-signed
```

- `MockProvider` covers dev and tests. `GenericHTTPProvider` uses `requests` with connect/read timeouts and `tenacity` retry on idempotent calls only.
- Provider credentials are stored encrypted (Fernet key from env `ARENA_SECRET_KEY`; `cryptography` is already installed).
- Completion and score claims are only pulled server-to-server and verified (signature or provider-side lookup). Client-reported completion is never accepted.
- SSRF control: admin-configured base URLs must be `https`, are resolved and checked against private/link-local ranges unless explicitly allowlisted, and redirects are disabled.

## 9. Core-modification gate

A core edit is allowed only when **all** of these hold: the plugin API cannot express it, a test shows the need, the patch is under about 30 lines, and it is listed in `MIGRATION_GUIDE.md`. Candidates identified now (**not planned unless proven necessary**):
- none for isolation (Modules cover it);
- *possible*: an env flag to skip plugin migrations in `load()` on web workers. This can probably be done inside our own plugin's `load()` instead, which would avoid a core change.

## 10. Activity tracking & analytics

- `activity.record(event_id, actor, action, target, meta)` → `RPUSH arena:activity` (capped with `LTRIM` to 100k). On Redis error the record is dropped and a counter incremented, so it never raises into the request.
- The worker flushes in batches of 500 into `arena_activity` (partitionable by month). A denylist strips keys such as `password`, `token`, `flag`, `submission`, `secret`.
- Native `tracking` stays as it is.
- Analytics read only from `arena_stats_*` rollup tables that the worker refreshes every 60 s. Dashboards never aggregate raw tables.

## 11. Certificates

Templates are a background image (native upload) plus a JSON field layout. Eligibility rules (`participation`, `rank<=N`, `award:<name>`, `custom`) are evaluated by the worker when an admin triggers issuance after the event ends. Rendering uses Pillow → PDF, stored through the native uploader. Each certificate gets a `uuid4` public id and an HMAC verification code. `/arena/verify/<public_id>` shows status (valid/revoked) without exposing private data. Revocation keeps the record and sets `revoked_at` and `reason`.

## 12. RBAC

Native `admin` = superadmin (all events). `arena_roles(user_id, event_id NULL|id, role)` with roles `event_admin`, `ad_operator`, `cert_manager`, `auditor` (read-only). These grant access only to arena admin pages and APIs; the native admin panel stays restricted to native admins.

## 13. Frontend

Plugin Jinja templates extend the active theme's `base.html`. They use one small CSS file and vanilla JS modules (no new framework), with countdowns computed client-side from server timestamps. Polling uses `ETag`, and scoreboard polling backs off exponentially when the tab is hidden.

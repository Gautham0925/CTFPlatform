# Database Design — Arena tables

Target: **MariaDB 10.11** (as in upstream compose), InnoDB, `utf8mb4_unicode_ci`. SQLite is supported for unit tests only.
All new tables use the prefix `arena_`. **No native table is altered.** The proposed logical entities map to physical tables like this:

| Brief entity | Table(s) | Native reuse |
|---|---|---|
| Event | `arena_events` | references native `modules` (sealed module) |
| EventRegistration | `arena_registrations` | FK `users` |
| EventTeam | `arena_teams`, `arena_team_members`, `arena_team_invites` | FK `users` |
| EventChallenge | `arena_event_challenges` | FK `challenges` (content stays native) |
| EventScore | `arena_score_totals` (+ derived Jeopardy) | — |
| ScoringEvent | `arena_score_ledger` | — |
| (scoring config) | `arena_scoring_configs`, `arena_recalc_runs` | — |
| Jeopardy submissions | `arena_submissions`, `arena_solves`, `arena_hint_unlocks` | FK `challenges`, `hints` |
| GameRound | `arena_ad_rounds` | — |
| ServiceDefinition | `arena_ad_services` | FK `arena_images` |
| ServiceInstance | `arena_ad_team_services` | — |
| AttackDefenseSubmission | `arena_ad_flags`, `arena_ad_flag_captures`, `arena_ad_checks`, `arena_ad_penalties` | — |
| Docker images/instances | `arena_images`, `arena_instances` | — |
| ExternalLabProvider / Session | `arena_lab_providers`, `arena_lab_sessions` | — |
| ActivityLog | `arena_activity` | — |
| CertificateTemplate / Certificate | `arena_cert_templates`, `arena_certificates` | files via native `files` / uploader |
| RBAC | `arena_roles` | FK `users` |
| Jobs | `arena_jobs` | — |
| Analytics | `arena_stats_event`, `arena_stats_challenge` | — |

`entity_key` = `t:<arena_team_id>` or `u:<user_id>` (VARCHAR(32)). Every score-bearing row also stores `event_id`. Timestamps are UTC `DATETIME(6)`.

## 1. Events & membership

```sql
arena_events(
  id PK, slug VARCHAR(64) UNIQUE, name, description TEXT, rules TEXT,
  type ENUM('jeopardy','ad','mixed'), status ENUM('draft','published','running','paused','ended','archived'),
  starts_at, ends_at, freeze_at NULL, registration_open BOOL, registration_opens_at NULL, registration_closes_at NULL,
  max_participants INT NULL, team_mode ENUM('individual','team'), team_size_min, team_size_max,
  requires_approval BOOL, visibility ENUM('public','unlisted','private'),
  sealed_module_id FK modules(id) ON DELETE RESTRICT UNIQUE,
  ad_enabled BOOL, jeopardy_enabled BOOL, scoring_config_id FK arena_scoring_configs NULL,
  secret_ref VARCHAR(64)   -- id of per-event HMAC secret (secret itself in env/secret store, never in DB)
  created_by FK users, created_at, updated_at,
  INDEX(status, starts_at))

arena_registrations(
  id PK, event_id FK CASCADE, user_id FK users CASCADE,
  status ENUM('pending','approved','rejected','withdrawn','banned'), source ENUM('self','admin','import'),
  approved_by FK users NULL, created_at,
  UNIQUE(event_id, user_id), INDEX(event_id, status))

arena_teams(
  id PK, event_id FK CASCADE, name VARCHAR(128), captain_user_id FK users SET NULL,
  status ENUM('active','banned','removed'), affiliation, created_at,
  UNIQUE(event_id, name), INDEX(event_id, status))

arena_team_members(
  id PK, event_id FK CASCADE, team_id FK arena_teams CASCADE, user_id FK users CASCADE,
  role ENUM('captain','member'), joined_at,
  UNIQUE(event_id, user_id),          -- one team per user per event
  INDEX(team_id))

arena_team_invites(
  id PK, event_id, team_id FK CASCADE, code_hash CHAR(64) UNIQUE, created_by, expires_at, max_uses, uses, revoked_at NULL)

arena_roles(
  id PK, user_id FK CASCADE, event_id FK NULL CASCADE, role ENUM('event_admin','ad_operator','cert_manager','auditor'),
  granted_by, created_at, UNIQUE(user_id, event_id, role))
```

A trigger-free consistency rule is enforced in the service layer and verified by the integrity job: `arena_team_members.event_id == arena_teams.event_id`, and the member has an `approved` registration.

## 2. Jeopardy

```sql
arena_event_challenges(
  id PK, event_id FK CASCADE, challenge_id FK challenges CASCADE UNIQUE,  -- one event per challenge
  points_override INT NULL, difficulty ENUM('easy','medium','hard','insane') NULL,
  visible_from NULL, visible_until NULL, state ENUM('hidden','visible','locked'),
  max_attempts_override INT NULL, prerequisites JSON NULL /* event challenge ids */, position INT,
  instance_image_id FK arena_images NULL, lab_provider_id FK NULL,
  INDEX(event_id, state, position))

arena_submissions(
  id PK BIGINT, event_id, challenge_id, entity_key, user_id, status ENUM('correct','incorrect','partial','ratelimited'),
  provided_hash CHAR(64), provided_preview VARCHAR(64) NULL /* incorrect only, admin-visible */, ip VARCHAR(46), created_at,
  INDEX(event_id, entity_key, challenge_id, created_at), INDEX(event_id, created_at))

arena_solves(
  id PK BIGINT, event_id, challenge_id, entity_key, user_id, submission_id FK, created_at,
  UNIQUE(event_id, challenge_id, entity_key),    -- duplicate-score guard
  INDEX(event_id, created_at), INDEX(event_id, entity_key))

arena_hint_unlocks(
  id PK, event_id, hint_id FK hints CASCADE, entity_key, user_id, cost INT, created_at,
  UNIQUE(event_id, hint_id, entity_key))
```

## 3. Scoring

```sql
arena_scoring_configs(
  id PK, event_id FK CASCADE, version INT, params JSON /* weights, factors, tie_break */, created_by, created_at,
  UNIQUE(event_id, version))         -- immutable rows; new version per change

arena_score_ledger(
  id PK BIGINT, event_id, entity_key,
  component ENUM('jeopardy','ad_attack','ad_defense','ad_sla','bonus','penalty','hint_cost'),
  points DECIMAL(12,3) NULL /* NULL = derived (dynamic jeopardy) */,
  source VARCHAR(32), source_ref VARCHAR(96), config_version INT, actor_user_id NULL, reason VARCHAR(255) NULL,
  created_at,
  UNIQUE(event_id, source, source_ref),          -- idempotency: replays are no-ops
  INDEX(event_id, entity_key, component), INDEX(event_id, created_at))

arena_score_totals(
  event_id, entity_key, component, points DECIMAL(14,3), last_change_at, last_ledger_id,
  PRIMARY KEY(event_id, entity_key, component))

arena_recalc_runs(
  id PK, event_id, from_config_version, to_config_version, actor_user_id, reason, status, before JSON, after JSON, created_at)
```

## 4. Attack & Defense

```sql
arena_ad_services(id PK, event_id, name, image_id FK arena_images, ports JSON, checker_ref VARCHAR(128), flag_lifetime_rounds INT, enabled, UNIQUE(event_id,name))
arena_ad_team_services(id PK, event_id, team_id, service_id, runner_instance_id VARCHAR(64), host, port, status, UNIQUE(service_id, team_id))
arena_ad_rounds(id PK, event_id, number INT, starts_at, ends_at, status ENUM('pending','active','closing','closed'), closed_at, UNIQUE(event_id, number), INDEX(event_id,status))
arena_ad_flags(id PK BIGINT, event_id, round_id, team_id, service_id, flag_hash CHAR(64) UNIQUE, expires_round INT, UNIQUE(round_id, team_id, service_id))
arena_ad_flag_captures(id PK BIGINT, event_id, flag_id FK, attacker_team_id, round_id, created_at, UNIQUE(flag_id, attacker_team_id), INDEX(event_id, round_id))
arena_ad_checks(id PK BIGINT, event_id, round_id, team_id, service_id, status ENUM('up','mumble','corrupt','down','error'), detail VARCHAR(255), runner_sig CHAR(64), created_at, UNIQUE(round_id, team_id, service_id))
arena_ad_penalties(id PK, event_id, team_id, round_id NULL, points, reason, actor_user_id, created_at)
```

## 5. Execution & labs

```sql
arena_images(id PK, name, version, digest CHAR(71) /* sha256:… */, cpu_millis, memory_mb, pids, ttl_seconds, ports JSON, validated_at NULL, enabled, UNIQUE(name, version), UNIQUE(digest))
arena_instances(id PK, uuid CHAR(36) UNIQUE, event_id, challenge_id NULL, image_id, entity_key, status ENUM('requested','starting','running','stopping','stopped','failed','expired'), host, port, expires_at, last_health_at, created_at,
  INDEX(event_id, entity_key, status), INDEX(status, expires_at))
  -- "one running instance per entity per challenge" enforced by service lock + partial check
arena_lab_providers(id PK, name UNIQUE, adapter VARCHAR(64), base_url, credentials_enc BLOB /* Fernet */, timeout_s, enabled, last_health JSON)
arena_lab_sessions(id PK, uuid UNIQUE, event_id, provider_id, entity_key, user_id, external_id, status, connection_enc BLOB, expires_at, completed_at NULL, created_at,
  UNIQUE(provider_id, external_id), INDEX(event_id, entity_key))
```

## 6. Activity, analytics, certificates, jobs

```sql
arena_activity(id PK BIGINT, event_id NULL, actor_user_id NULL, actor_type ENUM('user','admin','system','runner'), action VARCHAR(48), target_type VARCHAR(32), target_id VARCHAR(64), meta JSON, ip VARCHAR(46), created_at,
  INDEX(event_id, created_at), INDEX(actor_user_id, created_at), INDEX(action, created_at))
arena_stats_event(event_id, bucket_start DATETIME, registrations, active_users, active_teams, submissions, correct, PRIMARY KEY(event_id, bucket_start))
arena_stats_challenge(event_id, challenge_id, solves, attempts, unique_attempters, updated_at, PRIMARY KEY(event_id, challenge_id))
arena_cert_templates(id PK, event_id NULL, name, kind ENUM('participation','winner','top_n','award','custom'), background_file_id FK files NULL, layout JSON, created_at)
arena_certificates(id PK, public_id CHAR(36) UNIQUE, event_id, template_id, user_id, team_id NULL, kind, rank INT NULL, title, issued_at, file_location VARCHAR(255) NULL, verify_hmac CHAR(64), revoked_at NULL, revoke_reason NULL,
  UNIQUE(event_id, template_id, user_id))
arena_jobs(id PK BIGINT, kind VARCHAR(48), payload JSON, status ENUM('queued','running','done','failed','dead'), attempts, max_attempts, run_after, locked_by, locked_at, last_error TEXT, dedupe_key VARCHAR(128) UNIQUE NULL, created_at,
  INDEX(status, run_after))
```

## 7. Integrity, transactions, migrations

- **Transactions**: solve + submission + ledger + totals are written in one `db.session` transaction. A&D round close is one transaction per round. A row lock on `arena_ad_rounds` (`SELECT … FOR UPDATE`) prevents a double close.
- **Idempotency**: `UNIQUE(event_id, source, source_ref)` on the ledger, the unique keys on solves, captures and checks, and `arena_jobs.dedupe_key`.
- **Files**: no BLOBs except small encrypted credential/connection blobs (<4 KB). PDFs and templates go through the native uploader (filesystem/S3).
- **Migrations**: the Alembic chain lives in `CTFd/plugins/arena/migrations/`, applied through `CTFd.plugins.migrations.upgrade(plugin_name="arena")`. Every revision ships a `downgrade()`. Because the native helper has no downgrade runner, `flask arena db-downgrade <rev>` is provided. Because no native table is altered, rolling back is `downgrade base`, which drops `arena_*` tables and leaves native data untouched.
- **Retention**: `arena_activity` and `arena_submissions` are pruned by the worker per `ARENA_ACTIVITY_RETENTION_DAYS` (default 180) for archived events only.
- **Integrity job** (`flask arena check-integrity`, nightly): event challenges' `module_id == sealed_module_id`; sealed modules have zero audiences; team members' events match; ledger sums equal totals; solves have a ledger row.

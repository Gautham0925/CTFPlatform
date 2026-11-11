# Arena database schema

Arena adds 35 tables, all prefixed `arena_`. It never alters a native CTFd
table. Native rows Arena relies on are a **sealed** `modules` row per event
(one with no audience, so native CTFd answers 404 for it) and native
`challenges`/`flags`/`hints`/`files` created in that module with
`state="hidden"`. Native `/challenges`, `/api/v1/challenges`, native
scoreboards and `/files` therefore never expose event content.
`flask arena check-integrity` verifies that seal.

The tables below are generated from the SQLAlchemy models by
`tests/arena/tools/gen_reference_docs.py`.

## Invariants enforced by the database

| Invariant | Mechanism |
|---|---|
| One solve per event / challenge / entity | `UNIQUE uq_arena_solve_once` on `arena_solves (event_id, event_challenge_id, entity_key)` |
| A user is in at most one team per event | `UNIQUE uq_arena_members_event_user` on `arena_team_members (event_id, user_id)` |
| One active instance or lab session per entity and challenge | `UNIQUE uq_arena_inst_active` / `uq_arena_labs_active` on `active_key`, set while active and cleared on release |
| A score is written once per source | `UNIQUE uq_arena_score_source` on `arena_score_events (event_id, source, source_ref)` |
| Seats and team members never exceed capacity | conditional `UPDATE … SET seat_count = seat_count + 1 WHERE seat_count < cap` |
| State transitions are race-free | compare-and-set `UPDATE … WHERE status = :expected` |
| A job is processed by one worker | claim by CAS on `status`/lease; `UNIQUE uq_arena_jobs_dedupe` (key cleared when the job finishes) |
| Only one scheduler leader | lease row in `arena_leases` (DB-backed, survives a Redis outage) |

Lock ordering for submissions: the `arena_event_challenges` row
(`solve_count`) is updated **first**, then the solve and ledger rows are
inserted. MariaDB deadlocks (1213) and lock timeouts (1205) are retried.
Phase 4 tests ran 40 concurrent correct submissions and awarded the points
exactly once. After every Phase 16 load run there were no duplicate solves,
and the solve count matched the ledger.

## Scores are a ledger

`arena_score_events` is append-only: solves, first-blood bonuses, hint costs,
A&D captures, SLA and defense points, and manual adjustments (with actor and
reason). Standings are computed from it with the active versioned
`arena_scoring_configs` row (weights and tie-break). `arena_score_snapshots`
stores before/after standings whenever the config changes.
`GET …/scoreboard/verify` recomputes the standings from source rows.

## Migrations

| Revision | Adds |
|---|---|
| `a7e1f00d0001` | Arena foundation: events, registrations, roles |
| `a7e1f00d0002` | Phase 2: event lifecycle statuses, roles, branding/settings, capacity, invites |
| `a7e1f00d0003` | Phase 3: event teams, members and team invites |
| `a7e1f00d0004` | Phase 4: event challenges, submissions, solves, hint unlocks |
| `a7e1f00d0005` | Phase 5: scoring ledger, scoreboard snapshots, versioned scoring configs |
| `a7e1f00d0006` | Phase 6: background jobs, workers and leases |
| `a7e1f00d0007` | Phase 7: analytics rollup tables |
| `a7e1f00d0008` | Phase 8: challenge images, instances, event challenge image link |
| `a7e1f00d0009` | Phase 9: Attack and Defense |
| `a7e1f00d0010` | Phase 10: historical score snapshots |
| `a7e1f00d0011` | Phase 11: external lab providers, event labs, lab sessions |
| `a7e1f00d0012` | Phase 12: optional certificates |
| `a7e1f00d0013` | Phase 13: event announcements |

Use `flask arena migrate` to upgrade, `flask arena db-current` to inspect,
and `flask arena db-downgrade <rev|base> --yes` to step back (see
MIGRATION_GUIDE.md). On SQLite (dev/tests) the tables are created from the
models.

## Tables

### `arena_activity`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `event_id` | INTEGER | yes | FK → `arena_events.id` (SET NULL) |
| `actor_user_id` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `actor_type` | VARCHAR(16) | no |  |
| `action` | VARCHAR(48) | no |  |
| `target_type` | VARCHAR(32) | yes |  |
| `target_id` | VARCHAR(64) | yes |  |
| `meta` | JSON | yes |  |
| `created_at` | DATETIME | no |  |

Constraints: INDEX `ix_arena_activity_action_time` (action, created_at); INDEX `ix_arena_activity_actor_time` (actor_user_id, created_at); INDEX `ix_arena_activity_event_time` (event_id, created_at)

### `arena_ad_captures`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `flag_id` | BIGINT | no | FK → `arena_ad_flags.id` (CASCADE) |
| `attacker_team_id` | INTEGER | no | FK → `arena_teams.id` (CASCADE) |
| `victim_team_id` | INTEGER | no |  |
| `service_id` | INTEGER | no |  |
| `round_id` | INTEGER | no | FK → `arena_ad_rounds.id` (CASCADE) |
| `user_id` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `created_at` | DATETIME | no |  |

Constraints: INDEX `ix_arena_adc_event_round` (event_id, round_id); UNIQUE `uq_arena_adc_flag_attacker` (flag_id, attacker_team_id)

### `arena_ad_checks`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `round_id` | INTEGER | no | FK → `arena_ad_rounds.id` (CASCADE) |
| `team_id` | INTEGER | no | FK → `arena_teams.id` (CASCADE) |
| `service_id` | INTEGER | no | FK → `arena_ad_services.id` (CASCADE) |
| `status` | VARCHAR(16) | no |  |
| `detail` | VARCHAR(255) | yes |  |
| `created_at` | DATETIME | no |  |

Constraints: CHECK `ck_arena_adk_status`; INDEX `ix_arena_adk_event_team` (event_id, team_id); UNIQUE `uq_arena_adk_round_team_svc` (round_id, team_id, service_id)

### `arena_ad_flags`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `round_id` | INTEGER | no | FK → `arena_ad_rounds.id` (CASCADE) |
| `round_number` | INTEGER | no |  |
| `team_id` | INTEGER | no | FK → `arena_teams.id` (CASCADE) |
| `service_id` | INTEGER | no | FK → `arena_ad_services.id` (CASCADE) |
| `flag_hash` | VARCHAR(64) | no |  |
| `created_at` | DATETIME | no |  |

Constraints: UNIQUE `uq_arena_adf_hash` (flag_hash); UNIQUE `uq_arena_adf_round_team_svc` (round_id, team_id, service_id)

### `arena_ad_rounds`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `number` | INTEGER | no |  |
| `status` | VARCHAR(16) | no |  |
| `starts_at` | DATETIME | no |  |
| `ends_at` | DATETIME | no |  |
| `checks_done` | BOOLEAN | no |  |
| `closed_at` | DATETIME | yes |  |

Constraints: CHECK `ck_arena_adr_status`; INDEX `ix_arena_adr_event_status` (event_id, status); UNIQUE `uq_arena_adr_event_number` (event_id, number)

### `arena_ad_services`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `name` | VARCHAR(64) | no |  |
| `description` | TEXT | yes |  |
| `image_id` | INTEGER | no | FK → `arena_images.id` (RESTRICT) |
| `checker` | VARCHAR(255) | no |  |
| `attack_points` | INTEGER | no |  |
| `defense_points` | INTEGER | no |  |
| `loss_points` | INTEGER | no |  |
| `sla_points` | INTEGER | no |  |
| `enabled` | BOOLEAN | no |  |
| `created_at` | DATETIME | no |  |

Constraints: UNIQUE `uq_arena_ads_event_name` (event_id, name)

### `arena_ad_team_services`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `service_id` | INTEGER | no | FK → `arena_ad_services.id` (CASCADE) |
| `team_id` | INTEGER | no | FK → `arena_teams.id` (CASCADE) |
| `instance_id` | INTEGER | yes | FK → `arena_instances.id` (SET NULL) |
| `status` | VARCHAR(16) | no |  |
| `mock_status` | VARCHAR(16) | yes |  |
| `last_check_at` | DATETIME | yes |  |

Constraints: UNIQUE `uq_arena_adts_service_team` (service_id, team_id)

### `arena_announcements`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `title` | VARCHAR(128) | no |  |
| `body` | TEXT | no |  |
| `pinned` | BOOLEAN | no |  |
| `created_by` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `created_at` | DATETIME | no |  |

Constraints: INDEX `ix_arena_ann_event_time` (event_id, created_at)

### `arena_cert_templates`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `name` | VARCHAR(128) | no |  |
| `kind` | VARCHAR(16) | no |  |
| `title` | VARCHAR(128) | no |  |
| `body` | VARCHAR(1000) | no |  |
| `criteria` | JSON | yes |  |
| `accent_color` | VARCHAR(7) | no |  |
| `enabled` | BOOLEAN | no |  |
| `created_at` | DATETIME | no |  |

Constraints: CHECK `ck_arena_ctpl_kind`

### `arena_certificates`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `public_id` | VARCHAR(36) | no |  |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `template_id` | INTEGER | no | FK → `arena_cert_templates.id` (CASCADE) |
| `recipient_key` | VARCHAR(32) | no |  |
| `user_id` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `team_id` | INTEGER | yes | FK → `arena_teams.id` (SET NULL) |
| `recipient_name` | VARCHAR(128) | no |  |
| `team_name` | VARCHAR(128) | yes |  |
| `rank` | INTEGER | yes |  |
| `score` | INTEGER | yes |  |
| `kind` | VARCHAR(16) | no |  |
| `status` | VARCHAR(16) | no |  |
| `file_location` | VARCHAR(255) | yes |  |
| `verify_code` | VARCHAR(64) | no |  |
| `issued_at` | DATETIME | yes |  |
| `revoked_at` | DATETIME | yes |  |
| `revoke_reason` | VARCHAR(255) | yes |  |
| `created_at` | DATETIME | no |  |

Constraints: CHECK `ck_arena_cert_status`; INDEX `ix_arena_cert_event_user` (event_id, user_id); UNIQUE `uq_arena_cert_public` (public_id); UNIQUE `uq_arena_cert_recipient` (template_id, recipient_key)

### `arena_event_challenges`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `challenge_id` | INTEGER | no | FK → `challenges.id` (CASCADE) |
| `visible` | BOOLEAN | no |  |
| `enabled` | BOOLEAN | no |  |
| `difficulty` | VARCHAR(16) | yes |  |
| `position` | INTEGER | no |  |
| `scoring` | VARCHAR(16) | no |  |
| `points` | INTEGER | no |  |
| `minimum_points` | INTEGER | yes |  |
| `decay` | INTEGER | yes |  |
| `decay_function` | VARCHAR(16) | no |  |
| `current_value` | INTEGER | no |  |
| `solve_count` | INTEGER | no |  |
| `available_from` | DATETIME | yes |  |
| `available_until` | DATETIME | yes |  |
| `max_attempts` | INTEGER | yes |  |
| `prerequisites` | JSON | yes |  |
| `per_entity_flag` | BOOLEAN | no |  |
| `image_id` | INTEGER | yes | FK → `arena_images.id` (SET NULL) |
| `created_at` | DATETIME | no |  |
| `updated_at` | DATETIME | no |  |

Constraints: CHECK `ck_arena_ec_decay_fn`; CHECK `ck_arena_ec_difficulty`; CHECK `ck_arena_ec_points`; CHECK `ck_arena_ec_scoring`; INDEX `ix_arena_ec_event_visible_pos` (event_id, visible, position); UNIQUE `uq_arena_ec_challenge` (challenge_id)

### `arena_event_invites`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `code_hash` | VARCHAR(64) | no |  |
| `label` | VARCHAR(128) | yes |  |
| `max_uses` | INTEGER | yes |  |
| `uses` | INTEGER | no |  |
| `expires_at` | DATETIME | yes |  |
| `revoked_at` | DATETIME | yes |  |
| `created_by` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `created_at` | DATETIME | no |  |

Constraints: UNIQUE `uq_arena_invites_code` (code_hash)

### `arena_event_labs`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `provider_id` | INTEGER | no | FK → `arena_lab_providers.id` (RESTRICT) |
| `template_id` | VARCHAR(128) | no |  |
| `name` | VARCHAR(128) | no |  |
| `description` | TEXT | yes |  |
| `ttl_minutes` | INTEGER | no |  |
| `max_sessions` | INTEGER | no |  |
| `completion_points` | INTEGER | no |  |
| `enabled` | BOOLEAN | no |  |
| `created_at` | DATETIME | no |  |

Constraints: UNIQUE `uq_arena_elab_template` (event_id, provider_id, template_id)

### `arena_events`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `slug` | VARCHAR(64) | no |  |
| `name` | VARCHAR(128) | no |  |
| `description` | TEXT | yes |  |
| `rules` | TEXT | yes |  |
| `type` | VARCHAR(16) | no |  |
| `status` | VARCHAR(16) | no |  |
| `visibility` | VARCHAR(16) | no |  |
| `starts_at` | DATETIME | yes |  |
| `ends_at` | DATETIME | yes |  |
| `freeze_at` | DATETIME | yes |  |
| `registration_open` | BOOLEAN | no |  |
| `registration_opens_at` | DATETIME | yes |  |
| `registration_closes_at` | DATETIME | yes |  |
| `requires_approval` | BOOLEAN | no |  |
| `max_participants` | INTEGER | yes |  |
| `team_mode` | VARCHAR(16) | no |  |
| `team_size_min` | INTEGER | no |  |
| `team_size_max` | INTEGER | no |  |
| `branding` | JSON | yes |  |
| `settings` | JSON | yes |  |
| `seat_count` | INTEGER | no |  |
| `jeopardy_enabled` | BOOLEAN | no |  |
| `ad_enabled` | BOOLEAN | no |  |
| `sealed_module_id` | INTEGER | yes | FK → `modules.id` (RESTRICT) |
| `created_by` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `created_at` | DATETIME | no |  |
| `updated_at` | DATETIME | no |  |

Constraints: CHECK `ck_arena_events_status`; CHECK `ck_arena_events_team_mode`; CHECK `ck_arena_events_type`; CHECK `ck_arena_events_visibility`; INDEX `ix_arena_events_status_starts` (status, starts_at); UNIQUE `uq_arena_events_sealed_module` (sealed_module_id); UNIQUE `uq_arena_events_slug` (slug)

### `arena_hint_unlocks`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `event_challenge_id` | INTEGER | no | FK → `arena_event_challenges.id` (CASCADE) |
| `hint_id` | INTEGER | no | FK → `hints.id` (CASCADE) |
| `entity_key` | VARCHAR(32) | no |  |
| `user_id` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `cost` | INTEGER | no |  |
| `created_at` | DATETIME | no |  |

Constraints: INDEX `ix_arena_hint_event_entity` (event_id, entity_key); UNIQUE `uq_arena_hint_once` (event_id, hint_id, entity_key)

### `arena_images`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `name` | VARCHAR(64) | no |  |
| `version` | VARCHAR(32) | no |  |
| `reference` | VARCHAR(255) | no |  |
| `ports` | JSON | no |  |
| `cpu_millis` | INTEGER | no |  |
| `memory_mb` | INTEGER | no |  |
| `pids` | INTEGER | no |  |
| `ttl_seconds` | INTEGER | no |  |
| `enabled` | BOOLEAN | no |  |
| `validated_at` | DATETIME | yes |  |
| `created_at` | DATETIME | no |  |

Constraints: UNIQUE `uq_arena_images_name_version` (name, version); UNIQUE `uq_arena_images_reference` (reference)

### `arena_instances`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `uuid` | VARCHAR(36) | no |  |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `event_challenge_id` | INTEGER | yes | FK → `arena_event_challenges.id` (SET NULL) |
| `image_id` | INTEGER | no | FK → `arena_images.id` (RESTRICT) |
| `entity_key` | VARCHAR(32) | no |  |
| `user_id` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `purpose` | VARCHAR(16) | no |  |
| `status` | VARCHAR(16) | no |  |
| `active_key` | VARCHAR(96) | yes |  |
| `host` | VARCHAR(255) | yes |  |
| `ports` | JSON | yes |  |
| `error` | VARCHAR(255) | yes |  |
| `expires_at` | DATETIME | yes |  |
| `last_status_at` | DATETIME | yes |  |
| `created_at` | DATETIME | no |  |
| `updated_at` | DATETIME | no |  |

Constraints: CHECK `ck_arena_inst_status`; INDEX `ix_arena_inst_event_entity` (event_id, entity_key, status); INDEX `ix_arena_inst_status_exp` (status, expires_at); UNIQUE `uq_arena_inst_active` (active_key); UNIQUE `uq_arena_inst_uuid` (uuid)

### `arena_jobs`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `kind` | VARCHAR(48) | no |  |
| `payload` | JSON | yes |  |
| `status` | VARCHAR(16) | no |  |
| `priority` | INTEGER | no |  |
| `attempts` | INTEGER | no |  |
| `max_attempts` | INTEGER | no |  |
| `run_after` | DATETIME | no |  |
| `dedupe_key` | VARCHAR(128) | yes |  |
| `event_id` | INTEGER | yes | FK → `arena_events.id` (SET NULL) |
| `locked_by` | VARCHAR(96) | yes |  |
| `lease_until` | DATETIME | yes |  |
| `last_error` | VARCHAR(2000) | yes |  |
| `result` | JSON | yes |  |
| `created_at` | DATETIME | no |  |
| `started_at` | DATETIME | yes |  |
| `finished_at` | DATETIME | yes |  |

Constraints: CHECK `ck_arena_jobs_status`; INDEX `ix_arena_jobs_claim` (status, run_after, priority); INDEX `ix_arena_jobs_finished` (status, finished_at); INDEX `ix_arena_jobs_kind_status` (kind, status); UNIQUE `uq_arena_jobs_dedupe` (dedupe_key)

### `arena_lab_providers`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `name` | VARCHAR(64) | no |  |
| `adapter` | VARCHAR(32) | no |  |
| `base_url` | VARCHAR(255) | yes |  |
| `credentials_enc` | TEXT | yes |  |
| `config` | JSON | yes |  |
| `enabled` | BOOLEAN | no |  |
| `last_health` | JSON | yes |  |
| `last_health_at` | DATETIME | yes |  |
| `created_at` | DATETIME | no |  |

Constraints: CHECK `ck_arena_labp_adapter`; UNIQUE `uq_arena_labp_name` (name)

### `arena_lab_sessions`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `uuid` | VARCHAR(36) | no |  |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `event_lab_id` | INTEGER | no | FK → `arena_event_labs.id` (CASCADE) |
| `provider_id` | INTEGER | no | FK → `arena_lab_providers.id` (RESTRICT) |
| `entity_key` | VARCHAR(32) | no |  |
| `user_id` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `external_id` | VARCHAR(128) | yes |  |
| `status` | VARCHAR(16) | no |  |
| `active_key` | VARCHAR(96) | yes |  |
| `connection_enc` | TEXT | yes |  |
| `error` | VARCHAR(255) | yes |  |
| `expires_at` | DATETIME | yes |  |
| `completed_at` | DATETIME | yes |  |
| `created_at` | DATETIME | no |  |
| `updated_at` | DATETIME | no |  |

Constraints: CHECK `ck_arena_labs_status`; INDEX `ix_arena_labs_event_entity` (event_id, entity_key); INDEX `ix_arena_labs_status_exp` (status, expires_at); UNIQUE `uq_arena_labs_active` (active_key); UNIQUE `uq_arena_labs_uuid` (uuid)

### `arena_leases`

| Column | Type | Null | Notes |
|---|---|---|---|
| `name` | VARCHAR(64) | no | PK |
| `holder` | VARCHAR(96) | yes |  |
| `expires_at` | DATETIME | no |  |

### `arena_registrations`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `user_id` | INTEGER | no | FK → `users.id` (CASCADE) |
| `status` | VARCHAR(16) | no |  |
| `source` | VARCHAR(16) | no |  |
| `approved_by` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `invite_id` | INTEGER | yes | FK → `arena_event_invites.id` (SET NULL) |
| `created_at` | DATETIME | no |  |
| `updated_at` | DATETIME | no |  |

Constraints: CHECK `ck_arena_reg_source`; CHECK `ck_arena_reg_status`; INDEX `ix_arena_reg_event_status` (event_id, status); UNIQUE `uq_arena_reg_event_user` (event_id, user_id)

### `arena_roles`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `user_id` | INTEGER | no | FK → `users.id` (CASCADE) |
| `event_id` | INTEGER | yes | FK → `arena_events.id` (CASCADE) |
| `role` | VARCHAR(32) | no |  |
| `granted_by` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `created_at` | DATETIME | no |  |

Constraints: CHECK `ck_arena_roles_role`; UNIQUE `uq_arena_roles_user_event_role` (user_id, event_id, role)

### `arena_score_events`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `entity_key` | VARCHAR(32) | no |  |
| `user_id` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `component` | VARCHAR(16) | no |  |
| `points` | INTEGER | no |  |
| `source` | VARCHAR(32) | no |  |
| `source_ref` | VARCHAR(96) | no |  |
| `event_challenge_id` | INTEGER | yes | FK → `arena_event_challenges.id` (SET NULL) |
| `reason` | VARCHAR(255) | yes |  |
| `actor_user_id` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `config_version` | INTEGER | yes |  |
| `created_at` | DATETIME | no |  |

Constraints: CHECK `ck_arena_score_component`; INDEX `ix_arena_score_event_entity` (event_id, entity_key); INDEX `ix_arena_score_event_time` (event_id, created_at); UNIQUE `uq_arena_score_source` (event_id, source, source_ref)

### `arena_score_snapshots`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `kind` | VARCHAR(16) | no |  |
| `ref` | VARCHAR(64) | yes |  |
| `config_version` | INTEGER | yes |  |
| `payload` | JSON | no |  |
| `created_at` | DATETIME | no |  |

Constraints: INDEX `ix_arena_ssnap_event_time` (event_id, created_at)

### `arena_scoreboard_snapshots`

| Column | Type | Null | Notes |
|---|---|---|---|
| `event_id` | INTEGER | no | PK, FK → `arena_events.id` (CASCADE) |
| `scope` | VARCHAR(32) | no | PK |
| `version` | INTEGER | no |  |
| `payload` | JSON | no |  |
| `computed_at` | DATETIME | no |  |

### `arena_scoring_configs`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `version` | INTEGER | no |  |
| `params` | JSON | no |  |
| `reason` | VARCHAR(255) | yes |  |
| `created_by` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `created_at` | DATETIME | no |  |

Constraints: UNIQUE `uq_arena_scfg_event_version` (event_id, version)

### `arena_solves`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `event_challenge_id` | INTEGER | no | FK → `arena_event_challenges.id` (CASCADE) |
| `entity_key` | VARCHAR(32) | no |  |
| `user_id` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `submission_id` | BIGINT | yes | FK → `arena_submissions.id` (SET NULL) |
| `created_at` | DATETIME | no |  |

Constraints: INDEX `ix_arena_solve_event_entity` (event_id, entity_key); INDEX `ix_arena_solve_event_time` (event_id, created_at); INDEX `ix_arena_solve_event_user` (event_id, user_id); UNIQUE `uq_arena_solve_once` (event_id, event_challenge_id, entity_key)

### `arena_stats_challenge`

| Column | Type | Null | Notes |
|---|---|---|---|
| `event_id` | INTEGER | no | PK, FK → `arena_events.id` (CASCADE) |
| `event_challenge_id` | INTEGER | no | PK, FK → `arena_event_challenges.id` (CASCADE) |
| `attempts` | INTEGER | no |  |
| `incorrect` | INTEGER | no |  |
| `solves` | INTEGER | no |  |
| `unique_attempters` | INTEGER | no |  |
| `first_solve_at` | DATETIME | yes |  |
| `median_solve_seconds` | INTEGER | yes |  |
| `updated_at` | DATETIME | no |  |

### `arena_stats_hourly`

| Column | Type | Null | Notes |
|---|---|---|---|
| `event_id` | INTEGER | no | PK, FK → `arena_events.id` (CASCADE) |
| `bucket_start` | DATETIME | no | PK |
| `active_users` | INTEGER | no |  |
| `active_entities` | INTEGER | no |  |
| `submissions` | INTEGER | no |  |
| `correct` | INTEGER | no |  |
| `registrations` | INTEGER | no |  |
| `updated_at` | DATETIME | no |  |

### `arena_submissions`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | BIGINT | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `event_challenge_id` | INTEGER | no | FK → `arena_event_challenges.id` (CASCADE) |
| `entity_key` | VARCHAR(32) | no |  |
| `user_id` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `status` | VARCHAR(16) | no |  |
| `provided_hash` | VARCHAR(64) | no |  |
| `provided_preview` | VARCHAR(64) | yes |  |
| `ip` | VARCHAR(46) | yes |  |
| `created_at` | DATETIME | no |  |

Constraints: CHECK `ck_arena_sub_status`; INDEX `ix_arena_sub_entity_chal_time` (event_id, entity_key, event_challenge_id, created_at); INDEX `ix_arena_sub_event_status` (event_id, status); INDEX `ix_arena_sub_event_time` (event_id, created_at)

### `arena_team_invites`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `team_id` | INTEGER | no | FK → `arena_teams.id` (CASCADE) |
| `user_id` | INTEGER | no | FK → `users.id` (CASCADE) |
| `invited_by` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `status` | VARCHAR(16) | no |  |
| `pending_key` | VARCHAR(32) | yes |  |
| `expires_at` | DATETIME | yes |  |
| `created_at` | DATETIME | no |  |
| `responded_at` | DATETIME | yes |  |

Constraints: CHECK `ck_arena_tinv_status`; INDEX `ix_arena_tinv_user_status` (user_id, status); UNIQUE `uq_arena_tinv_pending` (pending_key)

### `arena_team_members`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `team_id` | INTEGER | no | FK → `arena_teams.id` (CASCADE) |
| `user_id` | INTEGER | no | FK → `users.id` (CASCADE) |
| `role` | VARCHAR(16) | no |  |
| `joined_at` | DATETIME | no |  |

Constraints: CHECK `ck_arena_members_role`; UNIQUE `uq_arena_members_event_user` (event_id, user_id)

### `arena_teams`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | INTEGER | no | PK |
| `event_id` | INTEGER | no | FK → `arena_events.id` (CASCADE) |
| `name` | VARCHAR(64) | no |  |
| `captain_user_id` | INTEGER | yes | FK → `users.id` (SET NULL) |
| `visibility` | VARCHAR(16) | no |  |
| `status` | VARCHAR(16) | no |  |
| `member_count` | INTEGER | no |  |
| `join_code_hash` | VARCHAR(64) | yes |  |
| `affiliation` | VARCHAR(128) | yes |  |
| `country` | VARCHAR(32) | yes |  |
| `created_at` | DATETIME | no |  |
| `updated_at` | DATETIME | no |  |

Constraints: CHECK `ck_arena_teams_member_count`; CHECK `ck_arena_teams_status`; CHECK `ck_arena_teams_visibility`; INDEX `ix_arena_teams_event_status` (event_id, status); UNIQUE `uq_arena_teams_event_name` (event_id, name); UNIQUE `uq_arena_teams_join_code` (join_code_hash)

### `arena_workers`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | VARCHAR(96) | no | PK |
| `hostname` | VARCHAR(64) | no |  |
| `pid` | INTEGER | no |  |
| `status` | VARCHAR(16) | no |  |
| `started_at` | DATETIME | no |  |
| `heartbeat_at` | DATETIME | no |  |
| `jobs_done` | INTEGER | no |  |
| `jobs_failed` | INTEGER | no |  |
| `current_job_id` | BIGINT | yes |  |
| `version` | VARCHAR(32) | yes |  |


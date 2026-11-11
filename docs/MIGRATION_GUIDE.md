# Arena Migration Guide

Arena schema changes are **never applied automatically**. CTFd's `load()` hook (and so every gunicorn worker) only *checks* the schema. If the schema is missing or behind, Arena routes answer `503` and `/arena/readyz` reports `"schema": false`. Native CTFd keeps working either way.

> SQLite (development/tests only) is the exception: like CTFd core and the bundled `dynamic_challenges` plugin, Arena calls `db.create_all()` on SQLite. SQLite is not supported in production.

## Commands

The commands need `DATABASE_URL` (and `SECRET_KEY`) set exactly as for the web app. They run in the app container (`docker compose run --rm ctfd …`) or a venv.

| Command | Effect |
|---|---|
| `flask arena db-current` | JSON: `up_to_date`, `head`, `current` |
| `flask arena migrate [--revision REV]` | Upgrade to head (or REV). Idempotent. |
| `flask arena db-downgrade REV --yes` | Downgrade to REV |
| `flask arena db-downgrade base --yes` | Remove **all** Arena tables. Native tables are untouched. |
| `flask arena check-integrity` | Read-only checks; exit code 1 on any problem |

The revision is stored in the native `config` table under `arena_alembic_version`, which is the same convention CTFd uses for plugin migrations (`CTFd/plugins/migrations.py`).

## Procedure (production)

1. **Back up**:
   ```bash
   mariadb-dump --single-transaction --routines --triggers ctfd > ctfd-$(date +%F).sql
   ```
2. **Rehearse on a copy**:
   ```bash
   mariadb -e "CREATE DATABASE ctfd_copy"
   ```
   ```bash
   mariadb ctfd_copy < ctfd-YYYY-MM-DD.sql
   ```
   ```bash
   DATABASE_URL=mysql+pymysql://…/ctfd_copy flask arena migrate
   ```
   ```bash
   DATABASE_URL=mysql+pymysql://…/ctfd_copy flask arena check-integrity
   ```
3. **Rollback rehearsal on the copy**: `flask arena db-downgrade base --yes`, then `flask arena migrate` again.
4. **Apply to production** with `flask arena migrate` (one process only, never from every replica), then run `flask arena check-integrity`.
5. Confirm `GET /arena/readyz` → `200`.

## Revisions

| Revision | Phase | Adds | Reversible |
|---|---|---|---|
| `a7e1f00d0001` | 1 | `arena_events`, `arena_registrations`, `arena_roles` | yes |
| `a7e1f00d0002` | 2 | lifecycle statuses, roles, branding/settings, capacity, `arena_event_invites` | yes |
| `a7e1f00d0003` | 3 | `arena_teams`, `arena_team_members`, `arena_team_invites` | yes |
| `a7e1f00d0004` | 4 | `arena_event_challenges`, `arena_submissions`, `arena_solves`, `arena_hint_unlocks` | yes |
| `a7e1f00d0005` | 5 | `arena_score_events`, `arena_scoreboard_snapshots`, `arena_scoring_configs` | yes |
| `a7e1f00d0006` | 6 | `arena_jobs`, `arena_workers`, `arena_leases` | yes |
| `a7e1f00d0007` | 7 | `arena_activity`, `arena_stats_hourly`, `arena_stats_challenge` | yes |
| `a7e1f00d0008` | 8 | `arena_images`, `arena_instances`, `arena_event_challenges.image_id` | yes |
| `a7e1f00d0009` | 9 | A&D: services, team services, rounds, flags, captures, checks | yes |
| `a7e1f00d0010` | 10 | `arena_score_snapshots` | yes |
| `a7e1f00d0011` | 11 | `arena_lab_providers`, `arena_event_labs`, `arena_lab_sessions` | yes |
| `a7e1f00d0012` | 12 | `arena_cert_templates`, `arena_certificates` | yes |
| `a7e1f00d0013` | 13 | `arena_announcements` | yes |

All of them are tested on MariaDB by `tests/arena/test_migrations.py` (upgrade,
downgrade to base, upgrade again, plus an upgrade from every previous
schema). Native rows are compared with `CHECKSUM TABLE`.

## Upgrading a Docker deployment

`deploy/scripts/upgrade.sh` runs these steps: backup → build → stop workers →
`flask db upgrade` + `flask arena migrate` (one-shot `migrate` service) →
`check-integrity` → restart → health check. It stops with the restore
command printed if the integrity check fails.

## Notes for 0.2.0

- **`ARENA_*` environment variables are now honoured.** Before 0.2.0, a
  variable set only in the environment (rather than in CTFd's config) was
  ignored, and Arena fell back to `SECRET_KEY`. If your environment sets
  `ARENA_SECRET_KEY`, then after the upgrade:
  - stored lab-provider tokens were encrypted under the old effective key
    (`SECRET_KEY`) and become unreadable. Re-enter them under Platform →
    Lab providers;
  - certificate verification codes of certificates issued earlier change.
    Re-issue the certificates if they must keep verifying.

  `ARENA_FLAG_SECRET` behaves the same way: per-entity flags of running
  events change. Upgrade between events, not during one.
- `ARENA_SECRET_KEY` must be at least 32 characters, otherwise the plugin
  refuses to load.

## What a downgrade leaves behind

`db-downgrade base` removes every `arena_*` table. Arena writes rows into native tables in two cases, and those rows are intentionally **kept**:

- one native `modules` row per event (`arena:<slug>`, no audience) — the sealed module;
- native `challenges`/`flags`/`hints`/`tags`/`files` rows of event challenges, which stay in their sealed module with `state = hidden`.

Deleting the sealed modules would set those challenges' `module_id` to NULL and **unseal** them, so they are kept. Every native row that existed before Arena is unchanged; `tests/arena/test_migrations.py` verifies this with `CHECKSUM TABLE` on MariaDB. To remove Arena content completely, delete the events' challenges in the Arena admin *before* downgrading.

## Core CTFd files modified

No application code. One repository config line: `.gitignore` gains `!CTFd/plugins/arena/`, because upstream ignores all non-bundled plugins (`CTFd/plugins/*`) and would otherwise silently exclude Arena from commits.

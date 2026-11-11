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

| Revision | Phase | Tables | Reversible |
|---|---|---|---|
| `a7e1f00d0001` | 1 | `arena_events`, `arena_registrations`, `arena_roles` | yes (drops only these tables) |

## Core CTFd files modified

No application code. One repository config line: `.gitignore` gains `!CTFd/plugins/arena/`, because upstream ignores all non-bundled plugins (`CTFd/plugins/*`) and would otherwise silently exclude Arena from commits.

# Existing CTFd Architecture (as checked out, v3.8.9 @ 485d59e7)

## 1. Process and request lifecycle

```
nginx (:80)  ──►  gunicorn 'CTFd:create_app()'  (gevent workers, WORKERS env)  ──►  MariaDB / Redis
                     docker-entrypoint.sh: ping.py → flask db upgrade → gunicorn
```

`create_app()` (`CTFd/__init__.py:189-356`) in order:

1. `CTFdFlask` with `CachingSessionInterface` (server-side sessions in cache) and sandboxed Jinja.
2. `cache.init_app` (Redis if `REDIS_URL`/`REDIS_HOST`, else filesystem — `CTFd/config.py:145-154`).
3. Jinja `ChoiceLoader`: overridden templates → current theme → fallback core theme → `admin/` / theme prefix → `plugins/` prefix.
4. `create_database()`, `db.init_app`, Flask-Migrate; SQLite → `create_all()`, else Alembic `upgrade()`.
5. `ProxyFix` if `REVERSE_PROXY`.
6. `init_request_processors` (setup redirect, **tracker**, banned check, forced password change, API token auth, **CSRF**, security headers).
7. Register blueprints: `views, teams, users, challenges, scoreboard, auth, api, events, social, admin`.
8. `init_logs`, `init_events` (SSE manager), **`init_plugins`**, `init_cli`.

Plugins load **after** all core blueprints (`CTFd/__init__.py:350-354`), alphabetically (`CTFd/plugins/__init__.py:get_plugin_names`), skipped entirely when `SAFE_MODE=true`.

## 2. Data model (native tables)

| Table | Purpose | Notable constraints |
|---|---|---|
| `users` (`Users`, polymorphic → `Admins`) | Accounts | `email` unique, `oauth_id` unique, **single `team_id` FK** |
| `teams` | Teams | `captain_id` FK users, `email` unique |
| `challenges` | Polymorphic on `type` (`standard`, `dynamic`, plugins) | `module_id` FK `modules` (SET NULL), `next_id` self-FK, `requirements` JSON |
| `dynamic_challenge` | Joined-table subtype | FK `challenges.id` CASCADE |
| `flags` | Polymorphic on `type` (`static`, `regex`) | FK challenge CASCADE |
| `hints`, `tags`, `topics`, `challenge_topics`, `solutions`, `files` (`challenge`/`page`/`solution` subtypes) | Content | CASCADE on challenge |
| `submissions` | Polymorphic on `type`: `correct`, `incorrect`, `partial`, `discard`, `ratelimited` | — |
| `solves` | Joined subtype of `submissions` | **UNIQUE(challenge_id,user_id), UNIQUE(challenge_id,team_id)** |
| `awards` | Manual awards / hint costs (negative) | — |
| `unlocks` (`hints`, `solutions`) | Paid unlocks | — |
| `tracking` | IP / UA per user | written on request path |
| `config` | Key/value config | — |
| `tokens` | API tokens | `value` unique |
| `notifications`, `pages`, `comments`, `fields`, `field_entries`, `brackets`, `ratings` | Misc | ratings UNIQUE(user,challenge) |
| `audiences`, `audience_members`, `modules`, `module_audience_access` | Content gating | UNIQUE(audience,user), (audience,team), (module,audience) |

Migrations: 35 core Alembic revisions in `migrations/versions/`. Plugins have independent chains stored as `config["<plugin>_alembic_version"]` (`CTFd/plugins/migrations.py`). The plugin helper provides `upgrade()` only; no downgrade helper.

## 3. Authentication, authorization, sessions

- `session["id"]` = user id; `session["hash"]` = HMAC of password hash → changing password invalidates sessions (`CTFd/utils/user/__init__.py:18-34`).
- Sessions are stored in the cache under `session<sid>`, lifetime `PERMANENT_SESSION_LIFETIME` (default 604800 s) (`CTFd/utils/sessions/__init__.py`).
- Decorators: `authed_only`, `admins_only`, `require_team`, `require_verified_emails`, `during_ctf_time_only`, `ratelimit`, visibility decorators (`check_challenge_visibility`, `check_score_visibility`, `check_account_visibility`, `check_registration_visibility`).
- API tokens: `Authorization: Token <value>` resolved in `before_request` (`init_request_processors.tokens`).
- CSRF: session nonce; API requires `CSRF-Token` header unless token-authenticated or `_bypass_csrf`.

## 4. Challenge → submission → score path

`POST /api/v1/challenges/attempt` (`CTFd/api/v1/challenges.py:660-1066`):

```
auth? ─► paused? ─► get user/team ─► teams-mode requires team
  ─► challenge state hidden/locked? ─► can_access_challenge (scheduled_at + module)
  ─► prerequisites satisfied?
  ─► SETNX lock submission_lock_{account}_{chal}  (only when max_attempts>0)
  ─► INCR account_kpm_{account}_{chal}  (rate limit, lockout/timeout)
  ─► already solved? (Solves query)
  ─► chal_class.attempt(challenge, request)      # flag validation only, no writes
  ─► correct: chal_class.solve() → INSERT solves (UNIQUE catches races) → decay recompute
     incorrect: chal_class.fail();  partial: chal_class.partial()
  ─► clear_standings(); clear_challenges()       # cache invalidation
```

Scoreboard: `get_standings()` / `get_team_standings()` — UNION of `solves⨝challenges` and `awards`, grouped per account, ordered by score desc, last-solve date asc, id asc; memoized 60 s and cleared on every solve. `/api/v1/scoreboard` cached 60 s.

## 5. Caching

Flask-Caching (Redis). Memoized: config, user/team attrs, scores, places, standings, challenge lists, solve counts, accessible module ids. Explicit invalidators in `CTFd/cache/__init__.py` (`clear_standings`, `clear_challenges`, `clear_user_session`, `clear_team_session`, `clear_accessible_module_ids`, …).

## 6. Events (SSE)

- `/events` (`CTFd/events/__init__.py`), authed, rate limited, disabled by `SERVER_SENT_EVENTS=false`.
- `RedisEventManager`: one Redis pub/sub listener greenlet per process on channel `ctf`; each SSE client gets a `defaultdict(Queue)`; 5 s ping.
- Used for admin notifications (`CTFd/api/v1/notifications.py:151`).

## 7. Plugin API surface (verified)

`CTFd/plugins/__init__.py`: `register_plugin_assets_directory`, `register_plugin_asset`, `override_template`, `override_function`, `register_plugin_script/stylesheet`, `register_admin_plugin_script/stylesheet`, `register_admin_plugin_menu_bar`, `register_user_page_menu_bar`, `bypass_csrf_protection`. Challenge types register in `CHALLENGE_CLASSES`, flags in `FLAG_CLASSES`. Plugins may register their own blueprints, Flask-RESTX namespaces on `CTFd_API_v1`, CLI commands, and models.

## 8. Themes

`CTFd/themes/core` (Vite build, `package.json`), `core-deprecated`, `admin`. Templates under `<theme>/templates`. Plugin templates are reachable through the `plugins/` prefix loader and may `{% extends "base.html" %}` of the active theme.

## 9. Deployment as shipped

`docker-compose.yml`: `ctfd` (port 8000 published), `nginx` (:80), `db` mariadb:10.11 (internal network), `cache` redis:4 (internal network). Default credentials are hard-coded (`ctfd/ctfd`), no HTTPS, `WORKERS=1`, `ctfd` port 8000 published directly (bypassing nginx). These are development defaults and are replaced in the custom deployment (see `SECURITY_PLAN.md`).

## 10. Tests

`tests/` (pytest, xdist). Helpers in `tests/helpers.py` (`create_ctfd`, `register_user`, `login_as_user`, `gen_*`). Uses SQLite by default via `TestingConfig`.

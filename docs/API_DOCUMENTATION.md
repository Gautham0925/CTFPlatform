# Arena API documentation

Every Arena endpoint is listed below. The route table is **generated from the
running application's URL map** by `tests/arena/tools/gen_reference_docs.py`, so it
matches the code. Authorization is also enforced dynamically by
`tests/arena/test_authz_matrix.py`. That test calls every route as an
anonymous visitor and as a signed-in outsider, and fails on any 2xx response
that is not on its allow-list.

## Conventions

- **Base path:** `/api/v1/arena`. The JSON envelope is `{"success": true, "data": …, "meta": …}`
  on success and `{"success": false, "errors": ["…"], "code": "…"}` on error.
  `code` is a stable machine-readable string, for example `slug_taken`,
  `registration_closed`, `ratelimited`, `locked` or `quota`.
- **Authentication:** the CTFd session cookie. State-changing requests
  (POST/PATCH/DELETE) must send `Content-Type: application/json` and the CTFd
  CSRF nonce in the `CSRF-Token` header. Non-JSON requests fall back to
  CTFd's form-nonce check and are rejected with 403 without it.
- **Status codes:** 400 validation (`invalid:<field>`), 401 not signed in,
  403 signed in but not allowed, 404 not found **or not visible to you**
  (hidden or draft events, other events' objects), 409 state conflicts, 429
  rate limits (per account; see `core/ratelimit.py`), 503 when the Arena
  schema is behind (run `flask arena migrate`).
- **Event scoping:** the event comes only from the URL. Every object in a
  request is looked up *within* that event, so an id from another event
  returns 404.
- **Pagination:** `?page=N&per_page=M` (default 25, max 100). The response
  `meta.pagination` holds `page`, `per_page`, `total` and `pages`.
- **Scoreboards:** cached per event version. Responses carry a weak `ETag`;
  send `If-None-Match` to get `304`. `refresh_seconds` says how often clients
  should poll.
- **Times:** ISO-8601 UTC with a `Z` suffix.

## Authorization column

| Value | Meaning |
|---|---|
| `public` | No decorator. Some of these check inside the handler (e.g. sign-in, roles); the sweep test verifies them |
| `event visible` | Event must be visible to the caller (public + published, or registered/role) |
| `participant` | Signed in, with an approved registration in the event (team mode: in a team) |
| `event: PERM` | Event role grants PERM (organizer, challenge_manager, scoreboard_operator, ad_operator, cert_manager, analyst) or superadmin |
| `global: PERM` | Platform-wide permission (superadmin only for `events.create`, `roles.manage`, `platform.manage`) |

## Routes

| Method | Path | Authorization | Rate limit (per account) | Summary |
|---|---|---|---|---|
| `GET` | `/api/v1/arena/admin/events` | public |  |  |
| `POST` | `/api/v1/arena/admin/events` | global: rbac.PERM_EVENTS_CREATE |  |  |
| `DELETE` | `/api/v1/arena/admin/events/<int:event_id>` | event: rbac.PERM_EVENT_MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>` | event: rbac.PERM_EVENT_VIEW_ADMIN |  |  |
| `PATCH` | `/api/v1/arena/admin/events/<int:event_id>` | event: rbac.PERM_EVENT_MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/activity` | event: rbac.PERM_AUDIT_READ |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/ad/captures` | event: rbac.PERM_AUDIT_READ |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/ad/checks` | event: rbac.PERM_AUDIT_READ |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/ad/provision` | event: OPERATE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/ad/rounds` | event: rbac.PERM_EVENT_VIEW_ADMIN |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/ad/rounds/<action>` | event: OPERATE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/ad/services` | event: rbac.PERM_EVENT_VIEW_ADMIN |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/ad/services` | event: OPERATE |  |  |
| `PATCH` | `/api/v1/arena/admin/events/<int:event_id>/ad/services/<int:service_id>` | event: OPERATE |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/ad/team-services/<int:ts_id>/mock-status` | event: OPERATE |  | Development only: set the result the MockRunner checker reports. |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/ad/uptime` | event: rbac.PERM_EVENT_VIEW_ADMIN |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/analytics/challenges` | event: READ |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/analytics/overview` | event: READ |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/analytics/refresh` | event: READ | limit "POST", limit=6, interval=60 |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/analytics/timeline` | event: READ |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/announcements` | event: rbac.PERM_EVENT_MANAGE |  |  |
| `DELETE` | `/api/v1/arena/admin/events/<int:event_id>/announcements/<int:ann_id>` | event: rbac.PERM_EVENT_MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/certificates` | event: rbac.PERM_EVENT_VIEW_ADMIN |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/certificates/<int:cert_id>/revoke` | event: MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/certificates/templates` | event: rbac.PERM_EVENT_VIEW_ADMIN |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/certificates/templates` | event: MANAGE |  |  |
| `PATCH` | `/api/v1/arena/admin/events/<int:event_id>/certificates/templates/<int:tpl_id>` | event: MANAGE |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/certificates/templates/<int:tpl_id>/issue` | event: MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/challenges` | event: VIEW |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/challenges` | event: MANAGE |  |  |
| `DELETE` | `/api/v1/arena/admin/events/<int:event_id>/challenges/<int:ec_id>` | event: MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/challenges/<int:ec_id>` | event: VIEW |  |  |
| `PATCH` | `/api/v1/arena/admin/events/<int:event_id>/challenges/<int:ec_id>` | event: MANAGE |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/challenges/<int:ec_id>/clone` | event: MANAGE |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/challenges/<int:ec_id>/files` | event: MANAGE |  |  |
| `DELETE` | `/api/v1/arena/admin/events/<int:event_id>/challenges/<int:ec_id>/files/<int:file_id>` | event: MANAGE |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/challenges/<int:ec_id>/flags` | event: MANAGE |  |  |
| `DELETE` | `/api/v1/arena/admin/events/<int:event_id>/challenges/<int:ec_id>/flags/<int:flag_id>` | event: MANAGE |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/challenges/<int:ec_id>/hints` | event: MANAGE |  |  |
| `DELETE` | `/api/v1/arena/admin/events/<int:event_id>/challenges/<int:ec_id>/hints/<int:hint_id>` | event: MANAGE |  |  |
| `PATCH` | `/api/v1/arena/admin/events/<int:event_id>/challenges/<int:ec_id>/hints/<int:hint_id>` | event: MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/challenges/meta` | event: VIEW |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/export` | event: rbac.PERM_EVENT_MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/export/<kind>.csv` | event: READ | limit "GET", limit=20, interval=60 |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/instances` | event: rbac.PERM_EVENT_VIEW_ADMIN |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/instances/<int:inst_id>/stop` | event: rbac.PERM_LABS_MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/invites` | event: rbac.PERM_REGISTRATIONS_MANAGE |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/invites` | event: rbac.PERM_REGISTRATIONS_MANAGE |  |  |
| `DELETE` | `/api/v1/arena/admin/events/<int:event_id>/invites/<int:invite_id>` | event: rbac.PERM_REGISTRATIONS_MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/labs` | event: rbac.PERM_EVENT_VIEW_ADMIN |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/labs` | event: rbac.PERM_LABS_MANAGE |  |  |
| `PATCH` | `/api/v1/arena/admin/events/<int:event_id>/labs/<int:lab_id>` | event: rbac.PERM_LABS_MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/labs/sessions` | event: rbac.PERM_EVENT_VIEW_ADMIN |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/labs/sessions/<int:sid>/release` | event: rbac.PERM_LABS_MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/registrations` | event: rbac.PERM_EVENT_VIEW_ADMIN |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/registrations` | event: rbac.PERM_REGISTRATIONS_MANAGE |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/registrations/<int:reg_id>/<action>` | event: rbac.PERM_REGISTRATIONS_MANAGE |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/score-adjustments` | event: rbac.PERM_SCORING_MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/score-events` | event: rbac.PERM_AUDIT_READ |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/scoreboard` | event: rbac.PERM_EVENT_VIEW_ADMIN |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/scoreboard/freeze` | event: rbac.PERM_SCORING_MANAGE |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/scoreboard/invalidate` | event: rbac.PERM_SCORING_MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/scoreboard/verify` | event: rbac.PERM_AUDIT_READ |  | Compare the served (cached) board with a fresh computation from the DB. |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/scoring/configs` | event: rbac.PERM_EVENT_VIEW_ADMIN |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/scoring/configs` | event: rbac.PERM_SCORING_MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/scoring/snapshots` | event: rbac.PERM_AUDIT_READ |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/scoring/snapshots` | event: rbac.PERM_SCORING_MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/scoring/snapshots/<int:snap_id>` | event: rbac.PERM_AUDIT_READ |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/status` | event: rbac.PERM_EVENT_MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/submissions` | event: VIEW |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/teams` | event: rbac.PERM_EVENT_VIEW_ADMIN |  |  |
| `GET` | `/api/v1/arena/admin/events/<int:event_id>/teams/<int:team_id>` | event: rbac.PERM_EVENT_VIEW_ADMIN |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/teams/<int:team_id>/<action>` | event: rbac.PERM_TEAMS_MANAGE |  |  |
| `POST` | `/api/v1/arena/admin/events/<int:event_id>/teams/<int:team_id>/members/<int:user_id>/remove` | event: rbac.PERM_TEAMS_MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/platform/images` | global: rbac.PERM_PLATFORM_MANAGE |  |  |
| `POST` | `/api/v1/arena/admin/platform/images` | global: rbac.PERM_PLATFORM_MANAGE |  |  |
| `PATCH` | `/api/v1/arena/admin/platform/images/<int:image_id>` | global: rbac.PERM_PLATFORM_MANAGE |  |  |
| `POST` | `/api/v1/arena/admin/platform/images/<int:image_id>/validate` | global: rbac.PERM_PLATFORM_MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/platform/jobs` | global: PLATFORM |  |  |
| `POST` | `/api/v1/arena/admin/platform/jobs/<int:job_id>/retry` | global: PLATFORM |  |  |
| `GET` | `/api/v1/arena/admin/platform/jobs/stats` | global: PLATFORM |  |  |
| `GET` | `/api/v1/arena/admin/platform/lab-providers` | global: PLATFORM |  |  |
| `POST` | `/api/v1/arena/admin/platform/lab-providers` | global: PLATFORM |  |  |
| `PATCH` | `/api/v1/arena/admin/platform/lab-providers/<int:pid>` | global: PLATFORM |  |  |
| `POST` | `/api/v1/arena/admin/platform/lab-providers/<int:pid>/test` | global: PLATFORM | limit "POST", limit=10, interval=60 |  |
| `GET` | `/api/v1/arena/admin/platform/runner/health` | global: rbac.PERM_PLATFORM_MANAGE |  |  |
| `GET` | `/api/v1/arena/admin/platform/workers` | global: PLATFORM |  |  |
| `GET` | `/api/v1/arena/admin/roles` | global: rbac.PERM_ROLES_MANAGE |  |  |
| `POST` | `/api/v1/arena/admin/roles` | global: rbac.PERM_ROLES_MANAGE |  |  |
| `DELETE` | `/api/v1/arena/admin/roles/<int:role_id>` | global: rbac.PERM_ROLES_MANAGE |  |  |
| `GET` | `/api/v1/arena/certificates/<public_id>/download` | public |  |  |
| `GET` | `/api/v1/arena/certificates/verify/<public_id>` | public | limit "GET", limit=30, interval=60 | Public verification. Requires the code printed on the certificate, so |
| `GET` | `/api/v1/arena/events` | public |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>` | event visible |  |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/ad/flags` | participant | limit "POST", limit=120, interval=60 |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/ad/rounds` | participant |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/ad/status` | participant |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/announcements` | event visible |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/challenges` | participant |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/challenges/<int:ec_id>` | participant |  |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/challenges/<int:ec_id>/attempt` | participant | limit "POST", limit=30, interval=60 |  |
| `DELETE` | `/api/v1/arena/events/<int:event_id>/challenges/<int:ec_id>/instance` | participant |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/challenges/<int:ec_id>/instance` | participant |  |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/challenges/<int:ec_id>/instance` | participant | limit "POST", limit=10, interval=60 |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/challenges/<int:ec_id>/instance/restart` | participant | limit "POST", limit=5, interval=60 |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/files/<int:file_id>` | participant |  |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/hints/<int:hint_id>/unlock` | participant |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/labs` | participant |  |  |
| `DELETE` | `/api/v1/arena/events/<int:event_id>/labs/<int:lab_id>/session` | participant |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/labs/<int:lab_id>/session` | participant |  |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/labs/<int:lab_id>/session` | participant | limit "POST", limit=10, interval=60 |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/labs/<int:lab_id>/session/reset` | participant | limit "POST", limit=5, interval=60 |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/my-activity` | participant |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/my-certificates` | participant |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/my-instances` | participant |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/my-invites` | participant |  |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/my-invites/<int:invite_id>/<action>` | participant |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/my-score` | event visible |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/my-submissions` | participant |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/my-team` | participant |  |  |
| `PATCH` | `/api/v1/arena/events/<int:event_id>/my-team` | participant |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/my-team/activity` | participant |  |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/my-team/captain` | participant |  |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/my-team/invites` | participant | limit "POST", limit=30, interval=60 |  |
| `DELETE` | `/api/v1/arena/events/<int:event_id>/my-team/invites/<int:invite_id>` | participant |  |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/my-team/join-code` | participant |  |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/my-team/leave` | participant |  |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/my-team/members/<int:user_id>/remove` | participant |  |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/register` | event visible | limit "POST", limit=10, interval=60 |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/scoreboard` | event visible |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/scoreboard/categories` | event visible |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/scoreboard/challenges` | event visible |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/scoreboard/history` | event visible |  |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/scoring` | event visible |  | Public scoring rules so participants can see how they are ranked. |
| `GET` | `/api/v1/arena/events/<int:event_id>/teams` | participant |  |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/teams` | participant | limit "POST", limit=10, interval=60 |  |
| `GET` | `/api/v1/arena/events/<int:event_id>/teams/<int:team_id>` | participant |  |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/teams/join` | participant | limit "POST", limit=10, interval=60 |  |
| `POST` | `/api/v1/arena/events/<int:event_id>/withdraw` | event visible |  |  |
| `GET` | `/api/v1/arena/events/by-slug/<slug>` | public |  |  |
| `POST` | `/api/v1/arena/events/join` | public | limit "POST", limit=10, interval=60 | Register with an invitation code (the only way into private events). |
| `GET` | `/api/v1/arena/me/events` | public |  |  |
| `GET` | `/arena` | public |  |  |
| `GET` | `/arena/admin` | public |  |  |
| `GET` | `/arena/admin/e/<int:event_id>` | public |  |  |
| `GET` | `/arena/admin/platform` | public |  |  |
| `GET` | `/arena/e/<slug>` | public |  |  |
| `GET` | `/arena/e/<slug>/<section>` | public |  |  |
| `GET` | `/arena/healthz` | public |  |  |
| `GET` | `/arena/join` | public |  |  |
| `GET` | `/arena/readyz` | public |  |  |
| `GET` | `/arena/verify/<public_id>` | public |  |  |
| `GET` | `/plugins/arena/assets/<path:path>` | public |  |  |

## Runner API (platform → runner host)

The runner is a separate service that runs on the runner host only. Every `/v1/*` request
is HMAC-SHA256 signed over this canonical string:

```
METHOD \n PATH(?query) \n UNIX_TIMESTAMP \n NONCE \n sha256_hex(BODY)
```

The signature travels in the headers `X-Arena-Key-Id`, `X-Arena-Timestamp`,
`X-Arena-Nonce` and `X-Arena-Signature`. The runner rejects unknown keys,
clock skew above 60 s, replayed nonces and bad signatures (constant-time
comparison). Transport is HTTPS, verified against `ARENA_RUNNER_CA`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/instances` | Create (idempotent on `instance_id`) `{instance_id, image, ports, memory_mb, cpu_millis, pids, ttl_seconds, env, labels}` |
| GET | `/v1/instances/<id>` | Status, endpoint and ports |
| GET | `/v1/instances?label_event=` | List active instances |
| POST | `/v1/instances/<id>/stop` · `/start` | Stop or restart |
| DELETE | `/v1/instances/<id>` | Destroy (idempotent) |
| POST | `/v1/images/check` | Whether an image reference is allow-listed (digest-pinned) |
| POST | `/v1/ad/check` | Run an A&D checker container against a target |
| POST | `/v1/reap` | Expire overdue and orphaned instances now |
| GET | `/v1/health` | Backend, capacity and active count |
| GET | `/livez` | **Unauthenticated** liveness probe; returns only `{"status":"ok"}` |

## External lab provider contract

`GenericHTTPProvider` (`CTFd/plugins/arena/labs/http.py`) talks to any lab
system that implements the REST contract below. Admins register a provider
with a base URL and an optional bearer token. The token is stored encrypted
(Fernet, key derived from `ARENA_SECRET_KEY`).

Requests send `Accept: application/json` and `Authorization: Bearer <token>`
when a token is configured. Bodies are JSON.

| Method | Path | Request | Response (2xx) |
|---|---|---|---|
| GET | `{base}/health` | — | `{"status": "ok"}` (any short string) |
| GET | `{base}/labs` | — | `{"labs": [{"id": str, "name": str, "description": str}]}` (max 500 used) |
| POST | `{base}/sessions` | `{"template_id", "session_id", "ttl_seconds", "owner"}` | `{"id": str, "status": STATUS}` |
| GET | `{base}/sessions/{id}` | — | `{"status": STATUS, "completed": bool}`; **404 = expired** |
| GET | `{base}/sessions/{id}/connection` | — | any of `type, host, port, url, username, password, note` |
| POST | `{base}/sessions/{id}/reset` | — | any 2xx |
| DELETE | `{base}/sessions/{id}` | — | any 2xx (idempotent; 404 counts as released) |

`STATUS` is one of `provisioning`, `ready`, `stopped`, `expired`, `failed`.
`session_id` is Arena's own id; use it to deduplicate retried creates.
`owner` is an opaque Arena reference, not personal data.

Platform-side guarantees and limits:
- **Transport:** `https://` only, except for hosts listed in
  `ARENA_LAB_ALLOWED_PRIVATE_HOSTS`. Every resolved address must be public
  unless the host is listed. This is checked when the provider is configured
  and again before every request.
- **Redirects** are never followed; a 3xx is an error.
- **Timeouts:** connect 3 s and read 10 s by default (per-provider
  `connect_timeout`, `read_timeout`).
- **Retries:** idempotent calls are retried `retries` (default 2) times with
  exponential back-off on timeouts, 5xx and 429. `POST /sessions/{id}/reset`
  is never retried. Session creation is retried, so providers must
  deduplicate on `session_id`.
- **Response size:** at most 1 MB; larger responses are rejected. Invalid JSON
  is an error.
- **Sanitising:** fields are truncated to fixed lengths, unknown statuses are
  rejected, and connection `url` values are only passed to participants when
  they start with `https://`, `http://`, `ssh://` or `rdp://`.
- **Credentials:** connection credentials go only to the session's owner and
  are never logged.
- **Polling:** a provider can be unavailable without breaking the event. The
  worker polls status (`labs-sync`, every 30 s), expires sessions
  (`labs-expire`) and health-checks providers (`labs-health`).

`MockProvider` (`labs/mock.py`) implements the same interface in-process for
development and tests.

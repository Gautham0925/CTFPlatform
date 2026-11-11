# Admin guide

Arena adds competitions to a normal CTFd install. CTFd's own admin panel at
`/admin` keeps working for users, the native CTF, themes and so on.
Competitions are managed at **`/arena/admin`**; there is also an **Arena**
link in the admin navbar.

## Who can do what

The CTFd **admin** account is the Arena *superadmin*. Only the superadmin can
create competitions, assign roles, and manage the platform (runner, images,
lab providers, jobs). Everyone else gets **per-event roles**:

| Role | Permissions in that event |
|---|---|
| organizer | everything below, plus event settings, registrations, teams and announcements |
| challenge_manager | view admin, create and edit challenges, flags, hints and files |
| scoreboard_operator | view admin, freeze the scoreboard, adjustments, scoring configs |
| ad_operator | view admin, A&D services, rounds and checkers |
| cert_manager | view admin, certificate templates and issuing (only when certificates are enabled) |
| analyst | view admin, audit log, analytics and CSV exports |

Assign roles with `POST /api/v1/arena/admin/roles` (`{user_id, role,
event_id}`). Every administrative action is recorded in the event's
**Audit log**.

## Running a competition

1. **Create.** On `/arena/admin`, under "Create a competition", choose the
   type (`jeopardy`, `ad`, `mixed`), team mode, visibility and time window.
   A new event starts as a **draft**: invisible, with its content sealed away
   from native CTFd.
2. **Settings tab.** Set the description and rules (Markdown, sanitized),
   registration window, capacity, approval requirement and team size. The
   *Advanced settings* JSON uses a closed schema (see
   CONFIGURATION_REFERENCE §3). An unknown key is an error, never silently
   ignored.
3. **Challenges tab.** Create static or dynamic challenges, flags (static,
   regex, case-insensitive, per-team via `per_entity_flags`), hints with
   costs, files, prerequisites and release windows. Clone challenges from
   other events. Container challenges reference a **digest-pinned image**
   that the superadmin registered under Platform → Challenge images.
4. **Publish.** Status flow: `draft → upcoming → active → paused/completed →
   archived`. `upcoming` needs a start and an end. The worker moves events
   between phases on their times (`sync-statuses` job).
5. **During the event:**
   - **Registrations / Teams:** approve, reject, enroll users, issue
     invitation codes (private events can only be joined with a code).
   - **Submissions:** live feed with filters.
   - **Scoreboard:** freeze or unfreeze, bonuses and penalties (reason
     required, written to the ledger), **Verify** (recomputes everything
     from source rows), and a new scoring version (weights / tie-break;
     before and after standings are kept).
   - **Labs & instances:** see and stop participants' container instances
     and lab sessions.
   - **Attack & Defense** (A&D events): services, rounds, checker status,
     uptime.
   - **Announcements:** shown on the event overview.
6. **After the event:** set it to `completed`, download CSV **Exports**
   (registrations, teams, submissions, solves, scores; formula-injection
   safe), issue certificates if enabled, then archive.

## Platform page (superadmin): `/arena/admin/platform`

- **System health:** DB, cache, schema readiness, worker heartbeats, runner health.
- **Dead jobs:** jobs that ran out of retries. Inspect and retry.
- **Challenge images:** register `repo@sha256:…` references, then
  **Validate** them against the runner's allow-list.
- **Lab providers:** `mock` or `generic_http` (see the provider contract in
  API_DOCUMENTATION.md). Tokens are write-only and stored encrypted.

## Operator commands

```bash
flask arena check-integrity          # schema, windows, sealed modules, roles, seats, teams, scoring
flask arena jobs stats               # queue depth per kind/status
flask arena jobs retry <id>          # requeue a dead job
flask arena worker-health            # exit 1 if no live worker
```

In Docker deployments, prefix these with
`docker compose -f deploy/compose/community.yml run --rm migrate`.

## Good practice

- Rehearse on a copy: create the event, solve every challenge with a test
  account, then click **Verify** on the scoreboard.
- Keep `scoreboard_refresh_seconds` at 15 or more for large events. Clients
  poll with ETags, so unchanged boards cost almost nothing.
- Stagger the start-of-event login if you can (for example, open the doors
  10 minutes early). Each login costs ~280 ms of server CPU (bcrypt; see
  PERFORMANCE_REPORT.md).

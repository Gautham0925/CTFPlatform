# Troubleshooting

Each entry gives the symptom, the likely cause, and what to do. Many entries
come from real failures found during Phase 15–16 testing.

## Startup and migrations

| Symptom | Cause | Fix |
|---|---|---|
| Arena API answers **503** `schema` / `/arena/readyz` reports `"schema": false` | Arena tables are behind the code | `flask arena migrate` (compose: `docker compose … run --rm migrate migrate`) |
| `worker: waiting for Arena schema...` forever | The `migrate` service failed | `docker compose … logs migrate` |
| Compose: `required variable MARIADB_PASSWORD is missing` | `deploy/.env` is missing or incomplete | `cp deploy/examples/platform.env.example deploy/.env && deploy/scripts/gen-secrets.sh deploy/.env` |
| `ArenaConfigError: ARENA_SECRET_KEY must be at least 32 characters` | Weak key | Generate a new one (`gen-secrets.sh`). If lab tokens were stored under the old key, re-enter them. |
| `ARENA_RUNNER_MODE='x' is not supported` | Typo | `mock` or `remote` |
| MariaDB **1553** during downgrade | An FK-backing index was dropped before its table | Fixed in the shipped migrations. Regenerate custom migrations without dropping FK indexes. |

## Integrity

`flask arena check-integrity` prints one `[OK]`/`[FAIL]` line per check:

| Failing check | Meaning / fix |
|---|---|
| `sealed_modules` / `NOT SEALED` | An event's native module (`arena:<slug>`) was given an audience, or a challenge was moved out of it, so native CTFd could expose it. **Act immediately:** remove the audience (`DELETE FROM module_audience_access WHERE module_id = <sealed id>`, or in CTFd's admin) and set the challenge's `module_id` back to the sealed module. The message names both ids. Then check the audit and native logs to find who changed it. |
| `seat_counts` / `teams` | Cached counters disagree with rows (e.g. after manual SQL). The message names the event or team. |
| `scoring` | Ledger rows without a source, or solves without ledger rows. Run *Verify* in the event's Scoreboard tab. |
| SQL error **1267 Illegal mix of collations** | Fixed. Older builds compared `CAST(id AS CHAR)` with a column under a different collation. Upgrade. |

## Performance and errors under load

| Symptom | Cause | Fix |
|---|---|---|
| Bursts of **502** with nginx `no live upstreams` / `Connection reset by peer` | nginx reused a keep-alive connection that gunicorn was closing. With `max_fails=1`, one reset ejected a replica for 10 s. | Fixed in the shipped config: gunicorn `--keep-alive 75` above nginx's upstream `keepalive_timeout 30s`, plus `max_fails=5 fail_timeout=5s`. Keep that ordering if you change either. |
| Logins slow at event start (seconds) | bcrypt costs ~280 ms of CPU per login on the test machine | More CPU or web replicas; stagger entry. See PERFORMANCE_REPORT.md. |
| Many **429** for players behind one IP | Native CTFd login/register limits and nginx limits are per IP | Raise `RATELIMIT_LOGIN`, set `ARENA_RATELIMIT_EXEMPT_CIDR` for the venue range. Arena's own limits are per account. |
| Everyone logged out after a Redis restart | Redis without persistence. CTFd keeps sessions in Redis. | The shipped `redis.conf` enables AOF on the `redis` volume. |
| **500** on most pages while Redis is down | Native CTFd opens sessions in Redis on every request | Restart Redis (`restart: unless-stopped`). Arena's own Redis use fails over to the DB. |
| Scoreboard updates late | The cache is per event version, and the browser polls every `scoreboard_refresh_seconds` | Expected. Check that the worker is alive (`flask arena worker-health`). |

## Worker and jobs

| Symptom | Fix |
|---|---|
| `worker-health` exits 1 | No heartbeat in `--max-age` seconds. Check `docker compose … logs worker`. A worker finishes its current job on SIGTERM (`stop_grace_period: 40s`). |
| `jobs stats` shows `dead` jobs | Inspect them on Platform → Dead jobs and retry with `flask arena jobs retry <id>` once the cause is fixed. |
| `QueueFull` errors | More than `ARENA_JOBS_MAX_QUEUED` jobs of one kind are queued, so workers are not keeping up. Add a worker replica. |
| Periodic tasks run twice | They should not: only the worker holding the DB lease (`arena_leases`) schedules. Check for a clock skew above the lease time. |

## Runner and instances

| Symptom | Fix |
|---|---|
| Instance stuck in `requested` | Platform cannot reach the runner. From a worker: `python -c` with `urllib` to `$ARENA_RUNNER_URL/livez` using `ARENA_RUNNER_CA`. Check the host firewall on 8443. |
| Runner logs `rejected request … missing signature headers` | Something unsigned called `/v1/*`. Only `/livez` is public. |
| Runner logs `bad signature` / `timestamp skew` | `ARENA_RUNNER_SECRET` differs from `ARENA_RUNNER_KEYS`, or the clocks differ by more than 60 s (run NTP on both hosts). |
| Image validation fails | The reference is not digest-pinned or not in the runner's `ARENA_RUNNER_ALLOWED_IMAGES`. |
| Containers can reach internal hosts | `docker-user.sh` is not applied, or was removed by a Docker restart. Re-run it. |
| `certificate verify failed` platform → runner | The certificate's SAN doesn't match `ARENA_RUNNER_URL`'s host, or `ARENA_RUNNER_CA` is wrong. `runner-tls.sh <hostname>` creates a matching pair. |

## Labs

| Symptom | Fix |
|---|---|
| Provider test fails with `blocked: …` | The base URL resolves to a private address. Add the host to `ARENA_LAB_ALLOWED_PRIVATE_HOSTS` only if it is really internal and trusted. |
| `redirects are not followed` | Point the base URL at the final address. |
| Stored token stopped working after an upgrade | `ARENA_SECRET_KEY` from the environment is now honoured (it was ignored before 0.2.0). Re-enter the provider token. |

## Restores

`restore.sh` refuses without `--yes` and on any checksum mismatch. After a
restore, players log in again (sessions are not backed up).

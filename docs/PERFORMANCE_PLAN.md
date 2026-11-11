# Performance Plan — target: 100 concurrent participants

**Nothing has been measured yet.** No capacity claim will be made until Phase 16 produces measured results.

## 1. Baseline observations from source

| Native behaviour | Cost | Plan |
|---|---|---|
| `get_standings` UNION + group-by over all solves, memoized 60 s, cleared on **every** solve (`CTFd/cache/__init__.py:133`) | During bursts, each solve busts the cache and the next request recomputes | Arena boards are per event; invalidation is debounced (recompute at most once per 2 s per event via a Redis `SET NX PX` guard; readers get the stale value meanwhile) |
| Tracker `before_request` may write on the request path | One write per new IP/UA per 5 min | Leave native as is; arena activity goes through the async buffer |
| SSE: one long-lived connection per participant (gevent) | 100 greenlets is fine, but every reconnect hits auth and rate limit | Arena uses ETag polling and does not open its own SSE stream |
| gunicorn `WORKERS=1` default | One process | Production: `WORKERS = 2–4` (gevent), `SECRET_KEY` set (required by entrypoint) |
| SQLAlchemy pool `max_overflow=20` (`CTFd/config.py:307`) | — | Explicit `pool_size=10`, `max_overflow=10` per worker; MariaDB `max_connections` ≥ workers×20 + worker + headroom |

## 2. Design rules

1. **Hot paths are O(1) or a single indexed query:** attempt (≤ 6 queries, 1 transaction), challenge list (cached per event + per-entity solved set), scoreboard (cached JSON blob per event, `ETag` = version counter).
2. **Never aggregate raw tables on dashboards.** Use the `arena_stats_*` rollups.
3. **Pagination everywhere**, max 100 rows; keyset pagination on `(created_at, id)` for activity and submissions.
4. **Bounded queues:** activity list capped with `LTRIM`; job table has `max_attempts` and a dead state; SSE is not used by arena.
5. **No synchronous PDF, lab or runner calls in request handlers longer than a 3 s timeout.** Instance creation returns `202` with status polling.
6. **Upload limits:** nginx `client_max_body_size` reduced from upstream's `4G` to `50m` for participant paths (admin upload location configurable).
7. **Timeouts:** gunicorn `--timeout 30`; outbound HTTP timeout (3 s connect, 10 s read); DB `wait_timeout` as upstream.
8. **Graceful shutdown:** gunicorn `--graceful-timeout 20`; the worker traps SIGTERM, finishes the current job and releases the leader lock.
9. **Health:** `/arena/healthz` (liveness, no I/O), `/arena/readyz` (DB `SELECT 1`, Redis `PING`, worker heartbeat age < 30 s, runner reachability — admin-only details).

## 3. Indexing (see `DATABASE_DESIGN.md`)

Every hot query has a covering or leading index: `arena_solves(event_id, entity_key)`, `arena_submissions(event_id, entity_key, challenge_id, created_at)`, `arena_score_totals` PK, `arena_event_challenges(event_id, state, position)`. These are verified with `EXPLAIN` in Phase 5.

## 4. Load test plan (Locust)

**Workload model** (per virtual user, think time 2–8 s):

| Action | Weight |
|---|---|
| Dashboard / challenge list | 35% |
| Challenge detail | 20% |
| Scoreboard poll (ETag) | 20% |
| Submission (90% wrong, 10% right) | 15% |
| Team view / ops | 5% |
| Lab/instance request (mocked runner) | 5% |

**Scenarios**

1. **Soak**: 100 logged-in users, 3 concurrent events, **60 min** sustained.
2. **Submission burst**: 100 users submit within 10 s, 3 times, 5 min apart.
3. **Scoreboard burst**: 100 users refresh the scoreboard within 5 s right after a solve.
4. **Login storm**: 100 logins in 30 s (rate limits tuned, not disabled).
5. **Concurrent lab allocation**: 100 instance requests in 10 s against the mock runner.
6. **Redis restart** during the soak (resilience check, results reported separately).

**Acceptance targets** (from the brief)

| Metric | Target |
|---|---|
| Normal request p95 | < 500 ms |
| Submission p95 | < 1 s |
| Scoreboard p95 | < 1 s |
| HTTP 5xx | < 0.1% |
| Duplicate accepted scoring records | 0 (verified by SQL after the run) |
| Lost score updates | 0 (ledger count == expected correct submissions) |
| Memory/queue growth | flat after warm-up (RSS sampled every 30 s; Redis `LLEN`; `arena_jobs` queued count) |

**Reporting:** hardware (CPU model/cores, RAM, disk, host vs VM), software versions, worker counts, full Locust CSV, p50/p95/p99 per endpoint, error breakdown, DB slow-query log, and the post-run integrity SQL output. Reports go to `loadtest/reports/<date>.md`.

Reference: this analysis container has 4 vCPU / 15 GB RAM. It is a development sandbox and **will not be used as evidence of production capacity**.

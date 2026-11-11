# Performance report (Phase 16)

Every number here was **measured** on the setup below. Nothing is
extrapolated. The raw Locust CSVs are not committed; re-run the suite with
the commands at the end.

## Test environment

| Item | Value |
|---|---|
| Machine | 1 VM: 4 vCPU Intel Xeon @ 2.10 GHz, 15 GB RAM, virtio disk |
| Topology | **Everything on that one VM**: the Profile C stack (nginx, 2 web replicas × 4 gevent workers, 2 job workers, MariaDB 10.11, Redis 7), the runner + Docker challenge containers, **and the Locust load generator** |
| Image | `arena-platform:local` built from `deploy/docker/platform.Dockerfile` (see the sandbox note in `deploy/README.md`) |
| Data | 3 active events × 20 challenges (4 dynamic), 150 users with registrations (users 1–100 in event A, 1–50 also in B, 101–150 in C) |
| Client | Locust 2.47, `tests/arena/load/locustfile.py`, HTTPS through nginx |
| Single-IP conditions | All load came from one IP, so for the tests `RATELIMIT_LOGIN=5000,5` and `ARENA_RATELIMIT_EXEMPT_CIDR=172.16.0.0/12` (the Docker gateway range). Arena's per-account limits stayed **on**. |

Because the load generator and the runner share the 4 vCPUs with the stack,
these results understate what the same stack does on dedicated hardware. They
are not a substitute for a test on your own production hardware.

## Scenario "Participant" (realistic player)

Each simulated player waits 2–6 s between actions, spread over the event
pages: scoreboard poll with ETag (25), challenge list (20), challenge view
(12), flag submission with 30 % correct (12), my-score (5), announcements
(5), HTML page (4), score history (2).

### Steady state: 100 concurrent users, 10 min

**15,183 requests, 0 failures, 25.3 req/s.**

| Endpoint | n | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|
| GET scoreboard (ETag) | 4,345 | 15 | 27 | 45 |
| GET challenges | 3,580 | 19 | 28 | 130 |
| GET challenge | 2,104 | 23 | 32 | 55 |
| POST attempt | 2,097 | 30 | 42 | 60 |
| GET my-score | 855 | 16 | 27 | 37 |
| GET announcements | 860 | 12 | 17 | 32 |
| GET event page (HTML) | 695 | 30 | 43 | 63 |
| POST /login (ramp-up, 10 users/s) | 100 | 710 | 1,600 | 2,000 |

CPU (`docker stats`, 100 % = one core): each web replica averaged 22 %
(peaks of 160 % during the login ramp), MariaDB 8 %, Redis 3 %, nginx 1 %.
Host load average was at most 1.55 on 4 vCPUs.

### Soak, 100 users, 30 min

Run 1 (`soak30`): **38,920 requests, 0 failures**, p50 17 ms, p95 30 ms,
p99 63 ms. Earlier runs had already solved the seeded challenges, so this
run was **read-heavy** (only 293 submissions). Memory before → after:
web replicas 404 → 414 MB, MariaDB 178 → 183 MB, worker-1 121 → 179 MB,
worker-2 107 → 113 MB.

Run 2 (`soak30b`, fresh events, normal submission mix): see below.

## Burst scenarios

| Scenario | Result |
|---|---|
| **Login storm**: 150 users, spawn rate 50/s, then scoreboard watching (90 s) | **0 failures** (2,805 requests). POST /login p50 3.6 s, p95 8.5 s, max 11.7 s. GET /login p95 13 s (it queues behind the logins on the same gevent workers). Scoreboard p50 stays at 14 ms. |
| **Submission storm**: 100 users submitting every 0.5–1.5 s for 3 min | **0 failures**, 9,701 submissions at **54 req/s**, p50 16 ms, p95 82 ms, p99 270 ms. 6,806 of them were throttled by the per-account limit (30/min), as designed. 850 correct solves, 0 duplicates. |
| **Container instances**: 30 users each start → wait for running → stop, for 4 min, with a 41 s runner outage in the middle | 476 full cycles, **every instance reached `running`**. Time to running p50 5.1 s, p95 6.2 s (this includes the worker's job pickup and the client's 1 s polling). Requests made during the outage completed once the runner returned (max 60 s). 141 POSTs returned 429 during the outage window; the script did not record whether quota or rate limit caused them. 0 containers left over. |

### Why logins are slow under a storm

CTFd hashes passwords with bcrypt (`bcrypt-sha256`, cost 12). One
verification took **280 ms of CPU** inside the container. A gevent worker is
blocked for the whole hash, so 4 vCPUs cap out at roughly 14 logins/s. 150
simultaneous logins therefore need at least ~11 s, which matches what we
measured. This is native CTFd behaviour. To mitigate it: more CPU or web
replicas, and staggered entry (open the doors early). Scoreboard and
challenge traffic were not affected.

## Fault injection (100 users, 12 min, `faults100b`)

| t | Fault | Effect on users |
|---|---|---|
| +1.5 min | `docker restart` Redis | Errors for about 1 s (CTFd's session layer cannot reach Redis). **No logouts**: sessions persist (AOF). |
| +3.5 min | Worker restart (SIGTERM, graceful) | none |
| +5.5 min | **SIGKILL** of web replica 1, restarted after 5 s | 1 × 502 (an in-flight POST). nginx moved traffic to replica 2. |
| +7.5 min | `docker restart` MariaDB | a few 500s within about 1 s |
| +9 min | Runner stopped for 60 s | none for these users (no instance traffic) |

**Total: 16,144 requests, 20 failures (0.12 %).** Every failure fell within
about a second of a fault. Afterwards the ledger had no duplicate solves, the
integrity check was all OK, and there were no dead jobs.

Before the Phase 16 fixes, the same test **failed 46.6 %** of requests:
Redis ran without persistence, so its restart logged everyone out. A separate
login-storm run also produced 9.8 % 502s from an nginx/gunicorn keep-alive
race. Both are fixed; see the SECURITY_GUIDE "issues found" table.

## Correctness under load

After every run, `post_check.sh` verified: solves = solve-ledger rows,
0 duplicate solves, every correct submission has exactly one solve,
`solve_count` matches the solve rows, 0 dead jobs, and `check-integrity` all
OK. Final state after all runs: 3,000 solves = 3,000 ledger rows.

## Capacity statement

On **this 4-vCPU VM, with the load generator on the same machine**, the
Profile C stack served **100 concurrent realistic players** with 0 errors
and p95 under 50 ms for every API endpoint, and sustained **54 flag
submissions/s**. **150 simultaneous logins** completed without errors but
took up to ~12 s.

These measurements do **not** show capacity for more users, other hardware,
or many simultaneous container instances (only 30 users were tested).
Measure on your own hardware before an event.

## Reproduce

The `tests/` directory is not shipped in the image, so copy the seed script
into a web container first. Use test stacks only: it creates accounts with a
known password.

```bash
docker cp tests/arena/load/seed.py arena-ctfd-1:/tmp/seed.py
```

```bash
docker exec -w /opt/CTFd -e PYTHONPATH=/opt/CTFd -e LOAD_PREFIX=load arena-ctfd-1 python /tmp/seed.py
```

```bash
ARENA_LOAD_HOST_HEADER=ctf.example.org locust -f tests/arena/load/locustfile.py Participant --headless -u 100 -r 10 -t 10m --host https://ctf.example.org --csv out/steady
```

Classes: `Participant`, `SubmitStorm`, `ScoreboardWatch` (login storm with
`-u 150 -r 50`), `InstanceBurst`. `ARENA_LOAD_PREFIX` selects the seeded
event set.

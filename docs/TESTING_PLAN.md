# Testing Plan

## 1. Layers

| Layer | Tooling | Where |
|---|---|---|
| Unit | pytest | `tests/arena/unit/` (scoring math, decay, eligibility, HMAC, SSRF validator, flag generation) |
| Integration (SQLite) | pytest + `tests/helpers.py` (`create_ctfd`, `register_user`, `login_as_user`, `gen_challenge`, `gen_flag`, …) | `tests/arena/integration/` |
| Integration (MariaDB) | Same tests, run with `DATABASE_URL=mysql+pymysql://…` against compose MariaDB 10.11 | CI job `arena-mariadb` (needed for real locking/constraint behaviour) |
| Migration | Alembic up → down → up on a **copy of a populated DB** (generated with upstream `populate.py`), row counts + checksums of native tables before/after | `tests/arena/migrations/` |
| Security | Authz matrix generator, race tests (threads against MariaDB), CSRF, SSRF, runner HMAC forgery/replay | `tests/arena/security/` |
| Runner | pytest against runner in docker-in-docker; escape checks | `runner/tests/` |
| Load | Locust | `loadtest/` |

## 2. Baseline (unmodified upstream, recorded 2026-11-11)

Environment: Python 3.11 venv, `requirements.txt` + pytest 8.4.2 / xdist / randomly, SQLite, 4 vCPU.

```
.venv/bin/python -m pytest -q -n 8 -p no:randomly tests
735 passed, 2 skipped, 1 error in 283.07s
ERROR tests/utils/test_uploaders.py — ModuleNotFoundError: No module named 'moto'
```

The error comes from a missing dev dependency (`moto`, S3 mock), not a code failure. CI will install the full `uv` dev group so the baseline is 0 errors. **Every phase must keep the native suite at baseline.**

## 3. Mandatory scenarios → tests

| # | Scenario | Test(s) |
|---|---|---|
| 1 | Existing CTFd works | full native suite, every phase |
| 2 | Registration/auth | native auth tests + arena event-registration tests (windows, caps, approval) |
| 3 | Multiple simultaneous events | two running events, interleaved submissions; boards independent |
| 4 | No cross-event access | generated matrix: every arena route × actor class; plus native endpoints on sealed challenges return 404 |
| 5 | Jeopardy scoring | static, regex, dynamic in-event; hint costs; freeze |
| 6 | A&D scoring | simulated 10-round game with fixed seed → golden totals |
| 7 | Mixed scoring | weight/tie-break golden tests |
| 8 | Docker lifecycle | create → healthy → restart → expire → reap; limits applied (inspect container config) |
| 9 | External labs | `MockProvider` contract suite; failure/timeout/retry; forged completion rejected |
| 10 | Scoreboard updates | solve → board version bump → ETag changes; debounce correctness |
| 11 | No duplicate points | 100 threads submit the same correct flag on MariaDB → 1 solve, 1 ledger row; same for A&D captures |
| 12 | Certificates | eligible issued, ineligible refused, revoked shows revoked, tampered id fails |
| 13 | Admin permissions | RBAC matrix (superadmin / event_admin A / event_admin B / auditor / user) |
| 14 | Migrations preserve data | populated DB checksum test |
| 15 | Redis failure | stop Redis mid-test: submissions still correct (DB constraint), activity dropped with counter, scoreboard falls back to DB with short timeout, no 500 storm |
| 16 | Job failure safety | inject exceptions mid round-close / mid-batch; rerun → totals identical (idempotent ledger) |

## 4. Load tests
See `PERFORMANCE_PLAN.md §4`. Results are reported with hardware and workload. **No capacity claim before passing.**

## 5. CI
- `make lint` (ruff) and the native suite
- arena SQLite suite
- arena MariaDB suite (compose service)
- migration suite
- runner suite (privileged CI runner only)

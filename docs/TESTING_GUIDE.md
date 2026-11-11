# Testing guide

The current counts for every suite are in IMPLEMENTATION_STATUS.md §Final.

## Suites

| Suite | Location | Needs |
|---|---|---|
| Arena unit + integration | `tests/arena/` | nothing (SQLite); some tests are MariaDB-only and skip on SQLite |
| Arena on MariaDB | same, with `TESTING_DATABASE_URL` | MariaDB 10.11 + (optional) Redis |
| Native CTFd with Arena force-loaded | `tests/` minus `tests/arena`, with `tests/arena/tools/force_plugins.py` | — |
| Runner unit | `runner/tests/test_runner.py` | runner venv |
| Runner ↔ real Docker | `runner/tests/test_docker_integration.py` | Docker daemon, root (iptables), local registry |
| Browser end-to-end | `tests/arena/e2e/run_e2e.py` (Playwright) | MariaDB, Chromium |
| Deployment | `tests/arena/test_deploy.py` | `docker compose` for config validation; MariaDB for the backup/restore round trip |
| Load | `tests/arena/load/locustfile.py` | running stack + Locust |
| Live security probes | `tests/arena/security/live_probe.py` | running test stack |

## Commands

Arena on SQLite:

```bash
.venv/bin/python -m pytest -q -p no:randomly tests/arena
```

Arena on MariaDB (create the `ctfd` user with rights to create and drop databases; each test makes its own uuid-named DB):

```bash
TESTING_DATABASE_URL='mysql+pymysql://ctfd:ctfd@localhost/ctfd' ARENA_TEST_REDIS_URL=redis://localhost:6379/15 .venv/bin/python -m pytest -q -p no:randomly tests/arena
```

Native CTFd suite with Arena loaded into every test app:

```bash
PYTHONPATH=tests/arena/tools .venv/bin/python -m pytest -q -n 3 -p no:randomly -p force_plugins tests --ignore=tests/arena
```

Runner:

```bash
cd runner && .venv/bin/python -m pytest -q tests/test_runner.py
```

The Docker integration tests are opt-in. They need root (iptables), a Docker daemon, and a digest-pinned test image (build `runner/tests/images/web` and push it to a local registry):

```bash
cd runner && sudo ARENA_RUNNER_DOCKER_TESTS=1 ARENA_TEST_IMAGE=127.0.0.1:5000/arena-test-web@sha256:<digest> .venv/bin/python -m pytest -q tests/test_docker_integration.py
```

Browser end-to-end (writes screenshots to the given directory):

```bash
TESTING_DATABASE_URL='mysql+pymysql://ctfd:ctfd@localhost/ctfd' .venv/bin/python tests/arena/e2e/run_e2e.py /tmp/arena-shots
```

Live security probes against a **test** stack:

```bash
.venv/bin/python tests/arena/security/live_probe.py https://127.0.0.1 arena.test
```

Load tests: see PERFORMANCE_REPORT.md § Reproduce.

Lint:

```bash
.venv/bin/ruff check CTFd/plugins/arena tests/arena runner && .venv/bin/ruff format --check CTFd/plugins/arena tests/arena runner
```

## Expected differences in the forced native run

Compared with upstream's plain native run (742 passed, 2 skipped), the
forced run differs in two known, explained ways. Nothing else may differ.

1. **Three more skips.** Native race-condition tests skip on SQLite. With
   Arena loaded they are identical; on MariaDB + Redis they pass.
2. **`tests/test_plugin_utils.py`: `test_register_admin_plugin_menu_bar`
   and `test_register_user_page_menu_bar` fail.** These native tests
   register a menu item and then assert it is at **index 0** of the
   plugin menu list. Arena registers "Competitions" and "Arena" first, so
   the test's item is at index 1. The same tests' assertions that the item
   is rendered in the page pass. Native tests are left unmodified.

## Writing tests

- Use `tests/arena/helpers.py`: `create_arena_ctfd()`, `gen_event()`
  (creates a sealed module like production), `gen_registration()`,
  `gen_role()`, `make_user()`.
- Concurrency tests must run on MariaDB. Start all threads at a
  `threading.Barrier` **before** they touch the DB pool.
- Every bug fix gets a regression test that fails on the old code.
- New routes must pass `test_authz_matrix.py`. If a route is meant to be
  public, add it to the allow-list with a comment explaining why.

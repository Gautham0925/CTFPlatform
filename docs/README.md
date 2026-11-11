# Arena: multi-event competitions on CTFd

Arena is a CTFd plugin (`CTFd/plugins/arena`) that turns one CTFd 3.8.9
install into a platform for many competitions: Jeopardy, Attack & Defense,
and mixed events. It adds per-event teams, container challenges on an
isolated runner host, external labs, live scoreboards, analytics, optional
certificates and an admin console. It **extends** CTFd without modifying
CTFd's code, and uses free, open-source components only.

## Quick start (local evaluation)

```bash
docker compose -f deploy/compose/dev.yml up -d --build
```

Open http://127.0.0.1:8000/setup, create the admin account, and go to
**Competitions → Admin** (`/arena/admin`).

For production, see `deploy/README.md` (Profiles B and C).

## Documentation

| Read this | For |
|---|---|
| [PROJECT_ANALYSIS](PROJECT_ANALYSIS.md) · [EXISTING_ARCHITECTURE](EXISTING_ARCHITECTURE.md) | What upstream CTFd provides and where its limits are |
| [CUSTOM_ARCHITECTURE](CUSTOM_ARCHITECTURE.md) · [PLUGIN_DOCUMENTATION](PLUGIN_DOCUMENTATION.md) | How Arena is built |
| [FINAL_REPORT](FINAL_REPORT.md) | The 20-section completion report + exact commands |
| [IMPLEMENTATION_STATUS](IMPLEMENTATION_STATUS.md) | What is done, per phase, with test counts |
| [INSTALLATION_GUIDE](INSTALLATION_GUIDE.md) · [DEPLOYMENT_GUIDE](DEPLOYMENT_GUIDE.md) · [../deploy/README.md](../deploy/README.md) | Installing and running it |
| [CONFIGURATION_REFERENCE](CONFIGURATION_REFERENCE.md) | Every setting |
| [ADMIN_GUIDE](ADMIN_GUIDE.md) · [PARTICIPANT_GUIDE](PARTICIPANT_GUIDE.md) | Using it |
| [API_DOCUMENTATION](API_DOCUMENTATION.md) | All routes, the runner API, the external lab provider contract |
| [DATABASE_SCHEMA](DATABASE_SCHEMA.md) · [DATABASE_DESIGN](DATABASE_DESIGN.md) · [MIGRATION_GUIDE](MIGRATION_GUIDE.md) | Data model and upgrades |
| [SECURITY_GUIDE](SECURITY_GUIDE.md) · [RUNNER_SECURITY](RUNNER_SECURITY.md) | Threat model, controls, test results, residual risks |
| [PERFORMANCE_REPORT](PERFORMANCE_REPORT.md) | Measured load-test results (with hardware and caveats) |
| [TESTING_GUIDE](TESTING_GUIDE.md) · [DEVELOPMENT_GUIDE](DEVELOPMENT_GUIDE.md) | Working on the code |
| [TROUBLESHOOTING](TROUBLESHOOTING.md) | Symptoms → fixes |
| [CHANGELOG](CHANGELOG.md) | Changes per version |

Phase 0 planning documents (PERFORMANCE_PLAN, SECURITY_PLAN, TESTING_PLAN,
IMPLEMENTATION_PLAN, DATABASE_DESIGN) are kept as written. Where the
implementation differs from them, the guides above describe what was
actually built.

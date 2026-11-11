# Deployment guide

The step-by-step reference lives with the files it describes:
**[`deploy/README.md`](../deploy/README.md)**. It covers Profiles A/B/C,
backups, monitoring, the firewall and the go-live security checklist. This
page explains how to choose a profile and size it, and how upgrades work.

## Choosing a profile

| You run… | Use |
|---|---|
| A trial on your laptop | **A** `compose/dev.yml` |
| A club or class CTF; Jeopardy only, or labs at an external provider | **B** `compose/community.yml`, one host |
| A public or high-stakes event, container challenges, A&D | **C** `community.yml + competition.yml` on the platform host **plus** a separate runner host (`compose/runner.yml`) |

Profile B → C is a configuration change, not a migration: add the overlay,
set `ARENA_RUNNER_*`, start the runner host.

## Sizing (based on PERFORMANCE_REPORT.md; measure your own)

| Measured on a 4 vCPU / 15 GB VM shared with the load generator | Profile C stack result |
|---|---|
| 100 concurrent players, realistic mix | 0 errors, API p95 < 50 ms |
| Flag-submission burst | 54 submissions/s sustained, p95 82 ms |
| 150 simultaneous logins | 0 errors, but up to ~12 s (bcrypt CPU) |

Rules of thumb that follow from where the CPU went:
- **Logins** are the expensive operation (~280 ms CPU each). Plan CPU for the
  peak *login* rate, not the request rate: roughly one vCPU per ~3.5 logins/s.
- **Scoreboards** are cheap thanks to ETag + per-version caching. Keep
  `scoreboard_refresh_seconds` ≥ 15.
- **MariaDB** stayed under 0.35 of a core even in the submission storm. Give
  it RAM for the buffer pool (`deploy/mariadb/*.cnf`).
- **Runner host:** size by concurrent instances × the image's `memory_mb`,
  plus headroom. The runner caps at `ARENA_RUNNER_MAX_INSTANCES`.

## Upgrades

```bash
deploy/scripts/upgrade.sh
```

It backs up, builds, stops workers, migrates once, runs the integrity check
and restarts. Read MIGRATION_GUIDE.md ("Notes for 0.2.0") first. Upgrade
**between** events: changes to flag secrets or keys affect running events.

## Day-of-event checklist

- `deploy/scripts/healthcheck.sh` is green, and the backup ran within the last hour.
- Rehearse: solve every challenge with a test account, then *Verify* the scoreboard.
- Venue NAT? Set `ARENA_RATELIMIT_EXEMPT_CIDR` and raise `RATELIMIT_LOGIN`.
- Open registration or login 10–15 minutes before the start to spread logins.
- Keep `docker compose … logs -f ctfd worker` and `/arena/admin/platform` open.
- Recovery behaviour you can expect (measured): a Redis or MariaDB restart
  causes about 1 s of errors; a killed web replica costs one in-flight
  request; worker restarts and short runner outages cause no user-visible errors.

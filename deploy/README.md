# Arena deployment profiles

Everything here uses free, open-source components only (Docker Engine +
Compose v2, MariaDB 10.11, Redis 7, nginx 1.28). Nothing calls a paid service.

| Profile | File(s) | For | TLS | Runner |
|---|---|---|---|---|
| **A** local dev | `compose/dev.yml` | laptops, evaluation | none, binds `127.0.0.1:8000` only | mock |
| **B** community | `compose/community.yml` | single host, small/medium events | nginx, your certificate | mock (Jeopardy, labs) |
| **C** competition | `community.yml` + `competition.yml`, and `runner.yml` on a **second host** | larger events, container challenges, A&D | nginx | remote, HTTPS + HMAC |

Sizing numbers in `competition.yml` are starting points, not measured
guarantees. See `docs/PERFORMANCE_REPORT.md` for what has actually been measured.

## Profile A

```bash
docker compose -f deploy/compose/dev.yml up -d --build
```

Open http://127.0.0.1:8000/setup. The credentials in `dev.yml` are throwaway.
Do not expose this profile to a network.

## Profile B

```bash
cp deploy/examples/platform.env.example deploy/.env
deploy/scripts/gen-secrets.sh deploy/.env          # fills every CHANGE_ME
$EDITOR deploy/.env                                 # ARENA_DOMAIN, ARENA_PUBLIC_URL
# TLS: put fullchain.pem + privkey.pem in deploy/nginx/certs/
deploy/scripts/install.sh
```

Then open `https://<domain>/setup`. Only nginx publishes ports (80 → 443
redirect, 443). MariaDB and Redis live on an internal network with no route
outside. `ctfd`, `worker` and `migrate` also join an `egress` network so they
can reach a remote runner and external lab providers.

Migrations run once, in the one-shot `migrate` service (native `flask db
upgrade`, then `flask arena migrate`). Web and worker containers start after it
succeeds. Workers never migrate.

**Certificates (Let's Encrypt, free):** nginx serves
`/.well-known/acme-challenge/` from the `acme` volume. Run certbot in webroot
mode against that volume, or terminate TLS somewhere else and copy the files
into `deploy/nginx/certs/`. `scripts/selfsigned-cert.sh` makes a 30-day
certificate for tests only.

## Profile C

On the **runner host**, which must be a different machine from the platform:

```bash
cp deploy/examples/runner.env.example runner/.env   # keys, digest-pinned images, bind IP
deploy/scripts/runner-tls.sh runner.internal.example
docker compose -f deploy/compose/runner.yml up -d --build
sudo runner/firewall/docker-user.sh                 # egress + ingress isolation
```

Copy `deploy/compose/runner-ca/ca.crt` to the platform host at the same path.
Move `runner-ca-key/ca.key` offline, and never put it on the platform host.
In `deploy/.env` on the platform set:

```
ARENA_RUNNER_URL=https://runner.internal.example:8443
ARENA_RUNNER_KEY_ID=platform1
ARENA_RUNNER_SECRET=<same secret as in ARENA_RUNNER_KEYS>
ARENA_RUNNER_CA=/run/arena/runner-ca/ca.crt
WORKERS=4
```

then:

```bash
ARENA_COMPOSE_FILES=deploy/compose/community.yml:deploy/compose/competition.yml \
  deploy/scripts/install.sh
```

The platform never receives the Docker socket. That is enforced by
`tests/arena/test_deploy.py`.

## Operations

| Task | Command |
|---|---|
| Health summary (exit 0 = healthy) | `deploy/scripts/healthcheck.sh` |
| Backup (DB in one transaction + uploads + manifest + SHA-256) | `deploy/scripts/backup.sh [dir]` |
| Restore (checks checksums, stops web/worker, migrates, runs the integrity check) | `deploy/scripts/restore.sh <backup-dir> --yes` |
| Upgrade (backup, build, stop workers, migrate, integrity check, restart) | `deploy/scripts/upgrade.sh` |
| Integrity check | `docker compose … run --rm migrate flask arena check-integrity` |
| Job queue | `docker compose … run --rm migrate flask arena jobs stats` |

Bare-metal installs use the same scripts with `ARENA_DEPLOY_MODE=direct` plus
`DB_HOST DB_PORT DB_USER DB_PASSWORD DB_NAME UPLOAD_DIR CTFD_DIR` (see
`scripts/lib.sh`). Redis is not part of backups. It holds CTFd login
sessions (persisted with AOF, so they survive a Redis restart), caches, locks
and a bounded activity buffer. After a full restore, players log in again.

Schedule backups with cron on the host, for example `15 * * * *
/opt/arena/deploy/scripts/backup.sh`, and copy them off the host. A backup you
have never restored is not a backup: rehearse `restore.sh` on a staging host.

## Monitoring and logs

- Liveness: `GET /arena/healthz`. Readiness (DB, cache and schema, booleans
  only): `GET /arena/readyz`, reachable through nginx for external monitors.
  Both answer before CTFd's setup redirect and import lock.
- Workers: `flask arena worker-health` exits non-zero when no heartbeat is
  newer than `--max-age` seconds. It is also the worker container healthcheck.
- Runner: `GET /livez` is unauthenticated and returns no data. Every
  `/v1/*` endpoint needs a signature.
- Logs: every container logs to stdout with json-file rotation (5 × 20 MB).
  nginx access logs leave out query strings. Arena activity records never store
  IP addresses, user agents or submitted flags.
- Admin UI: `/arena/admin/platform` shows workers, jobs and runner health.

## Firewall guidance

Platform host: allow inbound 80 and 443, plus SSH from your admin network
only. Docker publishes nothing else in Profiles B and C.

Runner host:
- Allow 8443 (control API) **only from the platform host's address**.
- Allow the challenge port range (`ARENA_RUNNER_PORT_RANGE`) from participants.
- Bind challenge ports to `ARENA_RUNNER_BIND_IP` (the public NIC).
- `runner/firewall/docker-user.sh` blocks challenge containers from reaching
  the host, private ranges and cloud metadata (`ARENA-EGRESS` in
  `DOCKER-USER`), and blocks them from the host's own services
  (`ARENA-INPUT`). Re-run it after every Docker restart; it is idempotent.
  `--remove` undoes it.

## Rate limits behind NAT

nginx applies coarse per-IP limits: auth 30/min, flag submissions 120/min and
API 30/s, each with a burst. CTFd and Arena also enforce per-account limits.
If many players share one public IP (a university or LAN venue), set
`ARENA_RATELIMIT_EXEMPT_CIDR` in `deploy/.env` to that range. nginx then
skips its per-IP limits for it. Arena's own limits on flag submissions, team,
instance and lab actions are per **account**, so one shared address never
throttles the whole room. CTFd's native login/registration limits stay per IP
(`RATELIMIT_LOGIN`, default 10 per 5 s); raise them in `deploy/.env` if a whole
venue logs in at once.

## Security checklist (before going live)

- [ ] `deploy/.env` was generated by `gen-secrets.sh`, is mode 600 and is not in git.
- [ ] `ARENA_SECRET_KEY` is set and differs from `SECRET_KEY`. Back it up:
      encrypted lab credentials cannot be recovered without it.
- [ ] A real TLS certificate is in place. `curl -I https://<domain>` shows HSTS.
- [ ] `/setup` is completed and the admin password is strong and unique.
- [ ] `ARENA_CERTIFICATES_ENABLED` is left `false` unless you need certificates.
- [ ] Profile C: the runner is on a separate host, the API port is limited to the
      platform IP, `docker-user.sh` has been applied, and every allowed image is
      pinned by digest.
- [ ] The runner's HMAC secret is at least 32 random characters and identical on both sides.
- [ ] `ARENA_LAB_ALLOWED_PRIVATE_HOSTS` is empty unless a lab provider really
      lives on a private network.
- [ ] Backups are scheduled, copied off-host, and a restore has been rehearsed.
- [ ] `flask arena check-integrity` reports OK.
- [ ] Host OS and Docker get automatic security updates. Images are rebuilt after base-image CVEs.
- [ ] Rate limits are adjusted for venue NAT, if you have one.

## What has been verified (in this repository's CI-like sandbox)

- Profiles A, B and C were brought up with real containers, and every service
  reported healthy.
- Through nginx/TLS: setup, event, challenge, registration, a correct solve and
  the scoreboard all worked. Readiness was correct before and after setup.
- B: a backup, then a destructive change, then `restore.sh` gave table
  checksums and upload hashes identical to the pre-backup state. A corrupted
  backup and a missing `--yes` were refused.
- C: two web replicas and two workers ran against a TLS runner verified with a
  private CA. A digest-pinned image was validated, an instance started
  (~1 s), was reachable, and was torn down through the API.
- Sandbox limitation: this environment blocks plain-HTTP egress and intercepts
  TLS inside build containers. The image was therefore built with the
  Dockerfile's `apt-get` lines removed and Python wheels taken from a
  pre-downloaded wheelhouse. Every other line was identical. The shipped
  Dockerfile's `apt-get` steps were not run here.

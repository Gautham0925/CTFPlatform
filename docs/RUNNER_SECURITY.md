# Runner security

The runner (`runner/`) is the **only** Arena component that talks to a
container engine. It runs on a **dedicated runner host**, never on the CTFd
host. The CTFd containers never get the Docker socket; the
`test_platform_never_gets_docker_socket_or_privileges` deployment test
enforces this, and the platform package does not even import the Docker SDK.

## Trust boundaries

```
participants ──HTTPS──▶ nginx ─▶ CTFd/Arena (platform host)
                                     │  HTTPS (private CA) + HMAC-SHA256, replay-protected
                                     ▼
participants ──TCP ports──▶ challenge containers ◀── runner (runner host, Docker socket)
```

- **Platform → runner:** TLS verified against `ARENA_RUNNER_CA`, and every
  request is signed (method, path+query, timestamp, nonce, body hash). The
  runner rejects unknown key ids, clock skew above 60 s, replayed nonces and
  bad signatures, using constant-time comparison. Only `/livez` is
  unauthenticated, and it returns nothing but `{"status":"ok"}`.
- **Runner → Docker:** through the Docker socket, so the runner container is
  effectively root on the runner host. That is why the runner host must run
  nothing else of value.

## Container hardening (every challenge instance)

Set in `runner/arena_runner/backends/docker.py::container_kwargs` and
verified against a real Docker daemon by
`runner/tests/test_docker_integration.py`:

| Control | Value |
|---|---|
| Image | must match the allow-list **by digest** (`repo@sha256:…`); tags are refused |
| Privileges | `privileged=False`, `cap_drop=ALL`, no `cap_add`, `no-new-privileges` |
| Filesystem | read-only root; `/tmp` and `/run` are tmpfs with `noexec,nosuid,nodev` and a size cap |
| Resources | memory = memswap (no swap), CPU quota, `pids_limit`, `nofile` ulimit, max TTL |
| Isolation | a private bridge network per instance with inter-container communication off; private IPC; no volumes, no devices; `init` reaps zombies |
| Ports | published only on `ARENA_RUNNER_BIND_IP` from a fixed range |
| Logging | json-file, 1 MB × 2 per container |
| User | the image's `USER`. Set `ARENA_RUNNER_FORCE_USER=65534:65534` to force a non-root uid for every challenge; uid/gid 0 are rejected |
| Runtime | optional `ARENA_RUNNER_DOCKER_RUNTIME=runsc` (gVisor) for a kernel boundary |
| Lifetime | a reaper removes expired and orphaned containers, also after runner restarts |

## Network isolation (`runner/firewall/docker-user.sh`)

Docker's own rules do not stop a container from reaching the host or the
private network. The script adds two chains:

- `ARENA-EGRESS` (in `DOCKER-USER`, i.e. FORWARD): containers may answer
  connections that players started. They may **not** reach RFC 1918, CGNAT,
  link-local (cloud metadata `169.254.169.254`), loopback or
  `ARENA_PLATFORM_IPS`, and they get no other egress unless
  `ARENA_ALLOW_INTERNET_EGRESS=1`.
- `ARENA-INPUT` (in INPUT): containers cannot reach services on the runner
  host itself (sshd, the Docker API, anything else). DOCKER-USER does not
  see this traffic, which is why the second chain exists.

The integration tests check that a challenge container cannot reach the host,
a host-side Redis or the metadata address. Run the script after every Docker
restart; it is idempotent and `--remove` uninstalls it.

## A&D checkers

Checkers run as containers too (`/v1/ad/check`), on the separate
`arena-checkers` network, with the same hardening and a short timeout. Flags
reach checkers as environment variables (`ARENA_FLAG`, `ARENA_ROUND`,
`ARENA_TARGET_HOST`, `ARENA_TARGET_PORTS`) and never appear in logs.

## Residual risks and recommendations

| Risk | Mitigation status |
|---|---|
| A kernel exploit inside a container | Not prevented by namespaces alone. Use gVisor (`runsc`) or a VM-per-runner for untrusted challenge authors. Keep the runner kernel patched. |
| Image runs as root inside the container | Reduced by dropping all capabilities, no-new-privileges and a read-only root. Set `ARENA_RUNNER_FORCE_USER`, or enable Docker `userns-remap` on the runner host. |
| Runner compromise means root on the runner host (Docker socket) | Dedicated host, API reachable only from the platform IP (host firewall), mTLS-equivalent HMAC, digest-pinned images. Rootless Docker is a further option. |
| HMAC secret leak lets an attacker start allow-listed images | Rotate with multiple `ARENA_RUNNER_KEYS` entries. The quota caps the damage: `ARENA_RUNNER_MAX_INSTANCES`. |
| Participant DoS through many instances | Per-entity, per-event and runner-wide quotas, plus per-account rate limits on start and restart. |

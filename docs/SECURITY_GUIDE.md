# Security guide

This guide covers the threat model, the controls Arena implements (with the
test that proves each one), what was tested in Phase 16 and what was not,
the issues found and fixed, and the residual risks. Container isolation is
covered in RUNNER_SECURITY.md.

## Threat model (short)

| Actor | Wants to | Main controls |
|---|---|---|
| Participant | see other events' or teams' flags, files, hidden challenges; score without solving; DoS | Event scoping from the URL only; sealed native modules; DB uniqueness for solves; per-account rate limits; quotas |
| Event staff (role holder) | act outside their event or permission | Per-event RBAC; superadmin-only platform permissions; audit log |
| Anonymous internet | reach admin/participant APIs, enumerate private events | 401/403/404, with 404 for invisible events; CSRF; nginx limits |
| Malicious challenge image / player in a container | escape, reach the platform or private networks | Runner on a separate host, hardened containers, egress/ingress firewall |
| Malicious lab provider or DNS | SSRF into the platform network, inject markup | https-only, public-address check (config + every request), no redirects, response caps, field sanitising |
| Network attacker | read or modify traffic | TLS 1.2+ at nginx; TLS + HMAC + nonce replay protection to the runner |

## Controls and evidence

| Control | Where | Evidence |
|---|---|---|
| Every Arena route has a guard; any 2xx for an anonymous user or a non-participant must be on an explicit allow-list | `core/authz.py`, `core/rbac.py` | `tests/arena/test_authz_matrix.py`: sweeps all 153 route/method pairs, twice |
| The event comes only from the URL; objects are looked up within that event | `scoped_get_or_404` | `test_authz.py`, `test_challenges.py`, live probe "challenge id from event A via event B URL → 404" |
| Invisible events return 404 | `event_view` | `test_events.py`, `test_ui.py` |
| Event content never leaks through native CTFd | sealed module per event, `state=hidden`, fail-closed `ensure_sealed_module` | `test_native_compat.py`, `check-integrity`, live probe "invisible in native /api/v1/challenges" |
| CSRF | CTFd nonce in `CSRF-Token`; non-JSON requests need the form nonce | live probes (403 without token, 403 for form-encoded) |
| Solve exactly once under concurrency | `UNIQUE uq_arena_solve_once`, lock ordering, deadlock retry | 40-way concurrent test; zero duplicates after every load run (2,702 solves = 2,702 ledger rows) |
| Per-account rate limits (NAT-safe) | `core/ratelimit.py` | `test_ratelimit.py`; 6,806 throttled submissions in the storm test, all per account |
| Output encoding | Markdown rendered then sanitized server-side; JS only sets `innerHTML` from `*_html` fields | `test_ui.py::test_javascript_only_uses_innerhtml_for_sanitized_fields`; live XSS probe |
| CSV export formula injection | cells starting with `= + - @`, tab or CR are prefixed with `'` | `test_activity_analytics.py` |
| Secrets at rest | lab tokens Fernet-encrypted (HKDF from `ARENA_SECRET_KEY`, at least 32 chars, enforced); flags HMAC-derived | `test_labs.py`, `test_config.py` |
| Privacy | activity log stores no IP, user agent or flag text; retention prune | `test_activity_analytics.py` |
| Runner API | TLS (private CA) + HMAC-SHA256 + 60 s skew window + nonce replay store; only `/livez` is unsigned | `runner/tests/test_runner.py`; live probe: unsigned `POST /v1/instances` → 401 |
| Platform never touches Docker | no socket mount, no Docker SDK in the platform | `test_deploy.py`, `test_execution.py::test_platform_never_imports_docker` |
| Transport | TLS 1.2/1.3 only, HSTS, `nosniff`, no server version, HTTP→HTTPS | live probes |
| Real client IP | nginx overwrites `X-Forwarded-For`; CTFd trusts exactly 1 hop | `test_deploy.py::test_nginx_overwrites_forwarded_for…` |

## Security testing performed (Phase 16)

| Test | Result |
|---|---|
| Dynamic authorization sweep, 153 route/method pairs × 2 identities | pass; the reviewed allow-lists are in the test: 20 route/method pairs may answer an anonymous visitor (public pages, public event data, health, assets, certificate verification) and 6 more a signed-in non-participant (self-registration and own-registration actions) |
| Live black-box probes (`tests/arena/security/live_probe.py`, 22 checks) against Profile C | **22/22 pass** |
| Bandit (static analysis) on `CTFd/plugins/arena` and `runner/` (14.5k LOC) | 0 high; 7 medium and 2 low, all triaged as false positives. One spot (`runner/store.update`) hardened with an allow-list anyway |
| pip-audit, platform `requirements.txt` | Findings only in **CTFd 3.8.9's own pinned dependencies**: Flask 2.1.3 (2), Werkzeug 2.2.3 (9), pydantic 1.6.2 (1), python-dotenv 0.13.0 (1), setuptools 83.0.0 (1). Not changed, because upgrading would change the native baseline; see residual risks |
| pip-audit, runner `requirements.txt` | Werkzeug 2.2.3 findings → **runner upgraded to Flask 3.1.3 / Werkzeug 3.1.9**; tests pass |
| Container hardening against a real Docker daemon | `runner/tests/test_docker_integration.py` (caps, read-only, noexec tmpfs, pids, firewall: host, host Redis and metadata unreachable) |

**Not performed:** an OWASP ZAP scan (its container image could not be
fetched from this sandbox), an nmap scan (not installed; exposure was
verified from the compose configuration and the probes instead), and an
independent third-party penetration test. Run ZAP baseline/full scans and an
external test before a high-stakes event.

## Issues found and fixed during Phases 15–16

| Issue | Severity | Fix |
|---|---|---|
| `ARENA_*` environment variables (secret key, flag secret, retention, lab allow-list, public URL) were silently ignored, so lab tokens were encrypted with `SECRET_KEY` | High (config/crypto) | `config.import_environment`; minimum key length enforced; tests |
| Per-IP rate limit on Arena endpoints: a venue NAT shared 30 submissions per minute | Medium (availability) | Per-account limiter |
| Released container instances kept consuming quota until TTL (participant lockout) | Medium (availability) | Quotas count `active_key`; regression test |
| The runner CA private key was placed in the directory mounted into platform containers | Medium | Key moved to `runner-ca-key/` (not mounted, git-ignored); test |
| Readiness probe followed the setup redirect, so it reported healthy before setup | Low | Probes answer before other hooks; test |
| Non-persistent Redis logged out all users on restart | Medium (availability) | AOF persistence + volume; test |
| SSRF docstring overstated DNS-rebinding protection | Doc | Corrected; listed below as residual risk |
| Integrity check crashed on production collation | Low | Python-side comparison; regression test |

## Residual risks

| Risk | Notes / recommendation |
|---|---|
| Inherited CVEs in CTFd 3.8.9's pinned Flask/Werkzeug and others | Track upstream CTFd releases and rebase Arena onto them. Arena does not pin these itself. The Werkzeug debugger is never enabled in deployments. |
| DNS rebinding window in lab-provider requests | The host is checked before each request, but `requests` resolves it again afterwards. Use `ARENA_LAB_ALLOWED_PRIVATE_HOSTS` sparingly; put providers on fixed public IPs; egress-filter the platform host if you can. |
| Redis outage → native CTFd returns 500 (sessions) | Arena's own Redis use degrades to the DB. Keep Redis local with a restart policy (it recovered in under 1 s in the fault test). |
| One login costs ~280 ms of CPU (bcrypt cost 12, native) | A login flood can saturate CPU. nginx `arena_auth` limits apply per IP; see PERFORMANCE_REPORT. |
| Challenge containers run as the image's user by default | Set `ARENA_RUNNER_FORCE_USER` or `userns-remap`; consider gVisor (see RUNNER_SECURITY). |
| Superadmin = CTFd admin | Protect the account (strong password; CTFd has no built-in MFA). |
| No MFA, no account lockout beyond rate limits | Native CTFd behaviour. |

## Operating securely

- Follow the checklist in `deploy/README.md` before going live.
- Rotate `ARENA_RUNNER_KEYS` by adding a new key id, switching the platform
  over, then removing the old one.
- **Changing `ARENA_SECRET_KEY` makes stored lab tokens unreadable and
  changes certificate verification codes.** Re-enter the tokens afterwards.
- After any manual SQL, run `flask arena check-integrity`.
- Report a vulnerability in upstream CTFd through its SECURITY.md process.
  Report Arena issues to the maintainers of this fork.

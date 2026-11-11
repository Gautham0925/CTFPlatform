# Security Plan

## 1. Threat model (summary)

| Actor | Goal | Primary controls |
|---|---|---|
| Participant | Read other events' data, score without solving, double-score, tamper with A&D scoring | Server-side event resolution + DB-checked membership; unique constraints; ledger; HMAC flags |
| Malicious team in A&D | Attack platform or other events instead of opponents | Runner isolation; per-event networks; egress filter |
| Compromised challenge container | Escape to host, reach DB/Redis/admin | Separate runner host, no socket in CTFd, dropped caps, userns, no route to platform networks |
| Rogue / compromised lab provider | Inject fake completions, SSRF pivot | Server-to-server pull, signature verification, URL allowlist, no redirects |
| Event admin (non-superadmin) | Escalate to other events or native admin | `arena_roles` scoped by `event_id`; native admin panel stays `admins_only` |
| Anonymous | Forge certificates, enumerate users | Random public ids + HMAC verify code; verify page shows only name/event/kind/status |

## 2. Controls by requirement

| Area | Control |
|---|---|
| **Authentication** | Native CTFd (bcrypt via passlib, session hash invalidation, email confirmation, reset tokens, login rate limit). Unchanged. |
| **Session** | Native server-side sessions. Production config: `SESSION_COOKIE_SECURE=true` (via HTTPS + `REVERSE_PROXY`), `SameSite=Lax`, `TRUSTED_HOSTS` set, lifetime shortened to 24 h for events. |
| **CSRF** | Arena routes go through the native CSRF `before_request`. No arena route uses `bypass_csrf_protection`, except the runner/lab callback endpoints, which use HMAC instead of cookies and reject cookie sessions. |
| **Authorization / IDOR** | Every handler gets the event from `resolve_event()` and the actor from `get_current_user()`. Every child lookup filters by `event_id` and, where relevant, by `entity_key`. Pattern enforced by a lint test that fails on `.query.get(` inside `arena/` handlers. |
| **Cross-event isolation** | Sealed modules (native endpoints 404) plus arena guards. Automated test matrix: every arena endpoint × {own event, other event, unregistered, banned, anonymous}. |
| **Privilege escalation** | Role checks are decorators backed by DB lookups, not session flags. Role grants are audited. Users cannot create roles for themselves. |
| **SQL injection** | SQLAlchemy ORM/Core with bound parameters only. A lint test rejects `text(` with f-strings inside `arena/`. |
| **Submission replay / duplicate scoring** | Unique constraints (`DATABASE_DESIGN.md §7`); Redis lock reduces contention but **correctness relies on the DB constraint**. A&D: flag capture unique per (flag, attacker), round-bound expiry. |
| **Flag secrecy** | Submissions stored as SHA-256 hash, plus an admin-only preview for incorrect answers. Flags never logged. Activity denylist strips sensitive keys. A&D flags stored hashed. |
| **File uploads** | Native uploader. Allowed extensions for certificate templates: png/jpg. Size limit enforced at nginx and in-app. Files served with `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`. |
| **Docker execution** | No Docker socket in any platform container (CI test asserts that `/var/run/docker.sock` is absent in the `ctfd` image/compose). Runner defaults: unprivileged, `cap_drop: ALL`, `no-new-privileges`, pids/mem/cpu limits, read-only rootfs, userns-remap, per-instance network, egress `DROP` to RFC1918/169.254.0.0/16/platform IPs, digest-pinned images. Optional gVisor `runsc` runtime. |
| **Runner ↔ platform auth** | HMAC-SHA256 over method+path+timestamp+nonce+body-hash, keys rotated via key id, ±60 s clock skew, Redis nonce cache (TTL 120 s). TLS required. |
| **External integrations (SSRF)** | HTTPS-only base URLs, DNS resolution checked against private ranges at **request time** (to prevent DNS rebinding), `allow_redirects=False`, response size cap, timeouts. |
| **Secrets** | `.env`, never in DB plaintext. Lab credentials Fernet-encrypted with `ARENA_SECRET_KEY`. Per-event A&D HMAC keys derived via HKDF from a master key. |
| **Certificates** | Issuance only by the eligibility engine (from DB facts), never from client input. `verify_hmac` binds public_id+user+event+kind. Revocation is immediate on the verify page. |
| **Activity logs** | Append-only from the app's perspective (no update/delete API). Admin actions always logged. Retention prune applies only to archived events. |
| **Admin APIs** | Rate limited; destructive operations (archive, recalc, revoke, terminate all) require a confirmation token and are audited. |
| **Headers** | Native `response_headers` plus nginx: HSTS, `X-Frame-Options DENY` on arena pages, CSP for arena templates (`script-src 'self' 'nonce-…'`). |

## 3. Deployment hardening (changes vs upstream compose)

- Do not publish `ctfd:8000`. Only nginx `:443` (and `:80` redirect) is published.
- DB and Redis only on the `internal: true` network (already true upstream). Redis `requirepass`. Non-default DB passwords from `.env`.
- TLS at nginx (certbot or provided certs).
- The runner is on a separate host. The platform reaches it outbound over HTTPS. The runner has no route to the platform's internal network.

## 4. Security review deliverable (Phase 16)

A checklist review of every item in the brief §19, plus targeted tests and a manual pentest pass. The pentest uses Burp: IDOR sweep across event ids, race conditions with Turbo Intruder single-packet attack on submission and registration, CSRF, and runner-callback forgery. Findings are reported in `SECURITY.md` with severity and status.

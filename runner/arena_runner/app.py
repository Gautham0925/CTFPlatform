"""Runner HTTP API (Flask). Every endpoint requires a valid HMAC signature.

POST   /v1/instances              create (idempotent on instance_id)
GET    /v1/instances/<id>         status + endpoint + resource usage
POST   /v1/instances/<id>/stop
POST   /v1/instances/<id>/start   (restart)
DELETE /v1/instances/<id>         destroy (idempotent)
GET    /v1/instances              list active (optional ?label_event=)
POST   /v1/images/check           {"image": ...} -> allowed?
POST   /v1/reap                   run the reaper now
GET    /v1/health
GET    /livez                     unauthenticated liveness probe (no data)
"""

import logging
import re
import threading
import time
import uuid

from flask import Flask, jsonify, request

from arena_runner import __version__, signing
from arena_runner.backends import make_backend
from arena_runner.backends.base import BackendError, InstanceSpec
from arena_runner.config import DIGEST_REF, RunnerConfig
from arena_runner.store import Store

log = logging.getLogger("arena_runner")
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
FORBIDDEN_ENV = {"LD_PRELOAD", "LD_LIBRARY_PATH", "DOCKER_HOST"}
MAX_BODY = 64 * 1024


def _err(status, message):
    r = jsonify({"success": False, "error": message})
    r.status_code = status
    return r


def create_app(config=None, backend=None, store=None):
    config = config or RunnerConfig.from_env()
    app = Flask("arena_runner")
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY
    store = store or Store(config.db_path)
    backend = backend or make_backend(config)
    app.extensions["runner"] = {"config": config, "store": store, "backend": backend}
    alloc_lock = threading.Lock()

    @app.before_request
    def _auth():
        if request.path == "/livez" and request.method in ("GET", "HEAD"):
            return None  # liveness only: no data, no state, no side effects
        path = request.full_path.rstrip("?")
        try:
            signing.verify(
                config.keys,
                request.method,
                path,
                request.get_data(),
                request.headers,
                store,
                max_skew=config.max_skew,
            )
        except signing.SignatureError as e:
            log.warning("rejected request %s %s: %s", request.method, request.path, e)
            return _err(401, "unauthorized")
        return None

    def _public(row):
        return {
            "instance_id": row["id"],
            "status": row["status"],
            "image": row["image"],
            "host": row["host"],
            "ports": row["ports"],
            "labels": row["labels"],
            "expires_at": int(row["expires_at"]),
            "created_at": int(row["created_at"]),
            "error": row["error"],
        }

    def _alloc_ports(count):
        used = store.used_ports()
        free = [p for p in range(config.port_min, config.port_max + 1) if p not in used]
        if len(free) < count:
            raise BackendError("no free ports")
        return free[:count]

    @app.post("/v1/instances")
    def create():
        data = request.get_json(silent=True) or {}
        try:
            instance_id = str(uuid.UUID(str(data.get("instance_id"))))
        except (ValueError, TypeError):
            return _err(400, "instance_id must be a UUID")
        existing = store.get(instance_id)
        if existing is not None:
            return jsonify(_public(existing))  # idempotent retry
        image = str(data.get("image", ""))
        if not DIGEST_REF.match(image):
            return _err(400, "image must be pinned by digest (name@sha256:...)")
        if image not in config.allowed_images or not backend.check_image(image):
            return _err(403, "image not allowed on this runner")
        try:
            ports = [int(p) for p in data.get("ports", [])][:8]
            if any(not 1 <= p <= 65535 for p in ports):
                raise ValueError
            ttl = min(int(data.get("ttl_seconds", 3600)), config.max_ttl_seconds)
            cpu = int(data.get("cpu_millis", 500))
            mem = int(data.get("memory_mb", 256))
            pids = int(data.get("pids", 128))
        except (TypeError, ValueError):
            return _err(400, "invalid ports/limits")
        if ttl < 60 or cpu < 50 or mem < 16 or pids < 8:
            return _err(400, "limits too small")
        if (
            cpu > config.max_cpu_millis
            or mem > config.max_memory_mb
            or pids > config.max_pids
        ):
            return _err(400, "limits above runner maximum")
        env = data.get("env") or {}
        if (
            not isinstance(env, dict)
            or len(env) > 16
            or any(
                not ENV_KEY.match(str(k)) or k in FORBIDDEN_ENV or len(str(v)) > 512
                for k, v in env.items()
            )
        ):
            return _err(400, "invalid env")
        labels = {
            k: str(v)[:64]
            for k, v in (data.get("labels") or {}).items()
            if re.match(r"^[a-z_]{1,32}$", str(k))
        }
        with alloc_lock:
            if store.count_active() >= config.max_instances:
                return _err(429, "runner capacity reached")
            try:
                host_ports = dict(zip(ports, _alloc_ports(len(ports))))
            except BackendError as e:
                return _err(429, str(e))
            store.insert(instance_id, image, labels, ttl)
            store.update(
                instance_id,
                ports={str(k): v for k, v in host_ports.items()},
                host=config.public_host,
            )
        spec = InstanceSpec(
            instance_id=instance_id,
            image=image,
            ports=ports,
            cpu_millis=cpu,
            memory_mb=mem,
            pids=pids,
            ttl_seconds=ttl,
            env=env,
            labels=labels,
        )
        try:
            ref = backend.create(spec, host_ports)
            row = store.update(instance_id, backend_ref=ref, status="running")
        except BackendError as e:
            row = store.update(
                instance_id, status="failed", error=str(e)[:255], ports={}
            )
            return jsonify(_public(row)), 502
        log.info("created instance %s (%s)", instance_id, image)
        return jsonify(_public(row)), 201

    def _load(instance_id):
        row = store.get(instance_id)
        return row

    @app.get("/v1/instances/<instance_id>")
    def get(instance_id):
        row = _load(instance_id)
        if row is None:
            return _err(404, "not found")
        if row["status"] in ("running", "stopped") and row["backend_ref"]:
            actual = backend.status(row["backend_ref"])
            if actual == "missing":
                row = store.update(
                    instance_id, status="failed", error="container disappeared"
                )
            elif actual != row["status"]:
                row = store.update(instance_id, status=actual)
        out = _public(row)
        if row["backend_ref"] and row["status"] == "running":
            out["usage"] = backend.stats(row["backend_ref"])
        return jsonify(out)

    @app.get("/v1/instances")
    def list_instances():
        rows = store.active()
        event = request.args.get("label_event")
        if event:
            rows = [r for r in rows if r["labels"].get("event") == event]
        return jsonify({"instances": [_public(r) for r in rows]})

    @app.post("/v1/instances/<instance_id>/stop")
    def stop(instance_id):
        row = _load(instance_id)
        if row is None:
            return _err(404, "not found")
        if row["backend_ref"]:
            backend.stop(row["backend_ref"])
        return jsonify(_public(store.update(instance_id, status="stopped")))

    @app.post("/v1/instances/<instance_id>/start")
    def start(instance_id):
        row = _load(instance_id)
        if (
            row is None
            or not row["backend_ref"]
            or row["status"] in ("removed", "expired", "failed")
        ):
            return _err(404, "not found")
        try:
            backend.start(row["backend_ref"])
        except BackendError as e:
            return _err(502, str(e))
        return jsonify(_public(store.update(instance_id, status="running")))

    @app.delete("/v1/instances/<instance_id>")
    def delete(instance_id):
        row = _load(instance_id)
        if row is None:
            return jsonify({"instance_id": instance_id, "status": "removed"})
        if row["backend_ref"]:
            backend.remove(row["backend_ref"])
        return jsonify(_public(store.update(instance_id, status="removed", ports={})))

    @app.post("/v1/images/check")
    def check_image():
        image = str((request.get_json(silent=True) or {}).get("image", ""))
        allowed = (
            bool(DIGEST_REF.match(image))
            and image in config.allowed_images
            and backend.check_image(image)
        )
        return jsonify({"image": image, "allowed": allowed})

    CHECK_STATUS = {0: "up", 1: "mumble", 2: "corrupt", 3: "down"}

    @app.post("/v1/ad/check")
    def ad_check():
        """Plant this round's flag and check each team service.

        Body: {"round": n, "event": id, "services": [{team_id, service_id,
        checker, host, ports, flag, mock_status?}]}. Flags only ever travel
        platform -> runner over the signed, TLS-protected channel."""
        data = request.get_json(silent=True) or {}
        items = data.get("services") or []
        if not isinstance(items, list) or len(items) > 2000:
            return _err(400, "services must be a list (max 2000)")
        out = []
        for it in items:
            try:
                team_id, service_id = int(it["team_id"]), int(it["service_id"])
            except (KeyError, TypeError, ValueError):
                return _err(400, "team_id and service_id are required")
            checker = str(it.get("checker") or "mock")
            if checker == "mock":
                status = (
                    it.get("mock_status")
                    if it.get("mock_status") in CHECK_STATUS.values()
                    else "up"
                )
                out.append(
                    {
                        "team_id": team_id,
                        "service_id": service_id,
                        "status": status,
                        "detail": "mock checker",
                    }
                )
                continue
            if not DIGEST_REF.match(checker) or checker not in config.allowed_images:
                out.append(
                    {
                        "team_id": team_id,
                        "service_id": service_id,
                        "status": "error",
                        "detail": "checker not allowed",
                    }
                )
                continue
            env = {
                "ARENA_TARGET_HOST": it.get("host") or "",
                "ARENA_FLAG": str(it.get("flag") or ""),
                "ARENA_ROUND": str(data.get("round")),
                "ARENA_TARGET_PORTS": ",".join(
                    f"{k}:{v}" for k, v in (it.get("ports") or {}).items()
                ),
            }
            try:
                code = backend.run_checker(checker, env, timeout=30)
            except BackendError as e:
                out.append(
                    {
                        "team_id": team_id,
                        "service_id": service_id,
                        "status": "error",
                        "detail": str(e)[:200],
                    }
                )
                continue
            out.append(
                {
                    "team_id": team_id,
                    "service_id": service_id,
                    "status": CHECK_STATUS.get(code, "error"),
                    "detail": f"exit {code}",
                }
            )
        return jsonify({"round": data.get("round"), "results": out})

    @app.post("/v1/reap")
    def reap_now():
        return jsonify(reap(store, backend))

    @app.get("/livez")
    def livez():
        return jsonify({"status": "ok"})

    @app.get("/v1/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "version": __version__,
                "backend": backend.name,
                "active": store.count_active(),
                "capacity": config.max_instances,
                "time": int(time.time()),
            }
        )

    return app


def reap(store, backend, now=None):
    """Remove expired instances and orphaned containers (also after restarts)."""
    removed, orphans = 0, 0
    for row in store.expired(now):
        if row["backend_ref"]:
            backend.remove(row["backend_ref"])
        store.update(row["id"], status="expired", ports={})
        removed += 1
    known = {r["backend_ref"] for r in store.active() if r["backend_ref"]}
    for ref in backend.managed_refs() - known:
        backend.remove(ref)  # container without an active record
        orphans += 1
    for row in store.active():
        if (
            row["backend_ref"]
            and row["status"] == "running"
            and backend.status(row["backend_ref"]) == "missing"
        ):
            store.update(row["id"], status="failed", error="container disappeared")
    return {"expired": removed, "orphans_removed": orphans}


def start_reaper(app, interval=None):
    ext = app.extensions["runner"]
    interval = interval or ext["config"].reap_interval
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            try:
                reap(ext["store"], ext["backend"])
            except Exception:  # noqa: BLE001
                log.exception("reaper failed")

    t = threading.Thread(target=loop, name="arena-reaper", daemon=True)
    t.start()
    return stop


def wsgi():
    """gunicorn 'arena_runner.app:wsgi()'"""
    app = create_app()
    reap(
        app.extensions["runner"]["store"], app.extensions["runner"]["backend"]
    )  # startup cleanup
    start_reaper(app)
    return app

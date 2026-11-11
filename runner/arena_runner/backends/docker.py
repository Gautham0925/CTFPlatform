"""Docker backend (runs ONLY on the isolated runner host).

All security-relevant container settings are produced by the pure function
``container_kwargs`` so they can be unit-tested without a Docker daemon.

Hardening defaults
* never privileged; all Linux capabilities dropped; no-new-privileges
* read-only root filesystem; small noexec tmpfs for /tmp and /run
* CPU (nano_cpus), memory (no swap), pids and open-files limits
* no bind mounts, no volumes, no devices, never the Docker socket
* one bridge network per instance with inter-container traffic disabled
* published ports bound to the runner's public interface only
* optional gVisor runtime (ARENA_RUNNER_DOCKER_RUNTIME=runsc)
Egress filtering (block RFC1918, link-local/metadata, platform hosts) is
enforced by host firewall rules in runner/firewall/docker-user.sh, applied to
the DOCKER-USER chain, because a container cannot be trusted to filter
itself.
"""

from arena_runner.backends.base import Backend, BackendError

LABEL = "arena.managed"
CHECKER_NETWORK = "arena-checkers"
TMPFS_OPTS = "rw,noexec,nosuid,nodev,size=64m"


def network_name(instance_id):
    return f"arena-{instance_id}"


def container_name(instance_id):
    return f"arena-{instance_id}"


def network_kwargs(instance_id):
    return {
        "name": network_name(instance_id),
        "driver": "bridge",
        "internal": False,  # published ports require a non-internal bridge
        "check_duplicate": True,
        "options": {
            "com.docker.network.bridge.enable_icc": "false",
            "com.docker.network.bridge.enable_ip_masquerade": "true",
        },
        "labels": {LABEL: "true", "arena.instance": instance_id},
    }


def container_kwargs(spec, config, host_ports):
    if spec.memory_mb > config.max_memory_mb or spec.cpu_millis > config.max_cpu_millis:
        raise BackendError("resource request above runner limits")
    if spec.pids > config.max_pids:
        raise BackendError("pids above runner limits")
    ports = {
        f"{int(cp)}/tcp": (config.bind_ip, int(hp)) for cp, hp in host_ports.items()
    }
    kwargs = {
        "image": spec.image,
        "name": container_name(spec.instance_id),
        "detach": True,
        "labels": {
            LABEL: "true",
            "arena.instance": spec.instance_id,
            **{f"arena.{k}": str(v)[:64] for k, v in spec.labels.items()},
        },
        "environment": {k: str(v) for k, v in spec.env.items()},
        "ports": ports,
        "network": network_name(spec.instance_id),
        "privileged": False,
        "cap_drop": ["ALL"],
        "cap_add": [],
        "security_opt": ["no-new-privileges:true"],
        "read_only": True,
        "tmpfs": {"/tmp": TMPFS_OPTS, "/run": TMPFS_OPTS},  # noqa: S108 (in-container tmpfs)
        "mem_limit": f"{int(spec.memory_mb)}m",
        "memswap_limit": f"{int(spec.memory_mb)}m",  # no swap
        "nano_cpus": int(spec.cpu_millis) * 1_000_000,
        "pids_limit": int(spec.pids),
        "ulimits": [{"Name": "nofile", "Soft": 1024, "Hard": 2048}],
        "ipc_mode": "private",
        "init": True,
        "restart_policy": {"Name": "no"},
        "log_config": {
            "Type": "json-file",
            "Config": {"max-size": "1m", "max-file": "2"},
        },
        "volumes": {},
        "devices": [],
        "hostname": "challenge",
    }
    if config.docker_runtime:
        kwargs["runtime"] = config.docker_runtime
    if config.force_user:
        kwargs["user"] = config.force_user
    return kwargs


class DockerBackend(Backend):
    name = "docker"

    def __init__(self, config, client=None):
        self.config = config
        if client is None:
            import docker  # only required on the runner host

            client = docker.from_env()
        self.client = client

    def _not_found(self):
        try:
            import docker

            return docker.errors.NotFound
        except ImportError:  # tests with a fake client
            return KeyError

    def check_image(self, image):
        return image in self.config.allowed_images

    def create(self, spec, host_ports):
        name = container_name(spec.instance_id)
        not_found = self._not_found()
        try:
            existing = self.client.containers.get(name)
            return existing.name  # idempotent
        except not_found:
            pass
        try:
            self.client.networks.get(network_name(spec.instance_id))
        except not_found:
            self.client.networks.create(**network_kwargs(spec.instance_id))
        try:
            container = self.client.containers.run(
                **container_kwargs(spec, self.config, host_ports)
            )
        except Exception as e:
            self.remove(name)
            raise BackendError(f"container start failed: {type(e).__name__}")
        return container.name

    def _get(self, ref):
        try:
            return self.client.containers.get(ref)
        except self._not_found():
            return None

    def status(self, ref):
        c = self._get(ref)
        if c is None:
            return "missing"
        c.reload()
        return "running" if c.status == "running" else "stopped"

    def stop(self, ref):
        c = self._get(ref)
        if c is not None:
            c.stop(timeout=5)

    def start(self, ref):
        c = self._get(ref)
        if c is None:
            raise BackendError("missing container")
        c.start()

    def remove(self, ref):
        instance_id = ref[len("arena-") :] if ref.startswith("arena-") else ref
        c = self._get(ref)
        if c is not None:
            c.remove(force=True)
        try:
            self.client.networks.get(network_name(instance_id)).remove()
        except self._not_found():
            pass

    def managed_refs(self):
        return {
            c.name
            for c in self.client.containers.list(
                all=True, filters={"label": f"{LABEL}=true"}
            )
        }

    def run_checker(self, image, env, timeout):
        """Run an allowlisted checker image against a team service.

        Checkers run on the dedicated "arena-checkers" network (keep it OUT of
        ARENA_CONTAINER_NETS so checkers can reach the runner's public IP),
        with the same hardening as challenge containers and a hard timeout.
        """
        if image not in self.config.allowed_images:
            raise BackendError("checker image not allowed")
        not_found = self._not_found()
        try:
            self.client.networks.get(CHECKER_NETWORK)
        except not_found:
            self.client.networks.create(
                name=CHECKER_NETWORK,
                driver="bridge",
                labels={LABEL: "true", "arena.role": "checkers"},
            )
        container = self.client.containers.run(
            image=image,
            detach=True,
            environment={k: str(v) for k, v in env.items()},
            network=CHECKER_NETWORK,
            privileged=False,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            read_only=True,
            tmpfs={"/tmp": TMPFS_OPTS},  # noqa: S108 (in-container tmpfs)
            mem_limit="128m",
            memswap_limit="128m",
            nano_cpus=500_000_000,
            pids_limit=64,
            labels={LABEL: "true", "arena.role": "checker"},
            restart_policy={"Name": "no"},
        )
        try:
            result = container.wait(timeout=timeout)
            return int(result.get("StatusCode", 4))
        except Exception:  # noqa: BLE001 - timeout or daemon error -> 'error'
            return 4
        finally:
            container.remove(force=True)

    def stats(self, ref):
        c = self._get(ref)
        if c is None:
            return {}
        s = c.stats(stream=False)
        mem = s.get("memory_stats", {})
        return {
            "memory_mb": round(mem.get("usage", 0) / 1048576, 1),
            "memory_limit_mb": round(mem.get("limit", 0) / 1048576, 1),
            "pids": s.get("pids_stats", {}).get("current"),
        }

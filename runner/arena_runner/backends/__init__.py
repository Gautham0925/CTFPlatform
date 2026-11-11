"""Execution backends. Each implements the Backend interface in base.py."""


def make_backend(config):
    if config.backend == "mock":
        from arena_runner.backends.mock import MockBackend

        return MockBackend(config)
    if config.backend == "docker":
        from arena_runner.backends.docker import DockerBackend

        return DockerBackend(config)
    raise ValueError(f"unknown backend {config.backend!r}")

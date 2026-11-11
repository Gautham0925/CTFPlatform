# Arena runner image. Deploy ONLY on the isolated runner host; this container
# needs the host Docker socket, which must never be given to the CTFd host.
FROM python:3.11-slim-bookworm
WORKDIR /opt/runner
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -u 1001 -m runner \
    && mkdir -p /var/lib/arena-runner && chown runner /var/lib/arena-runner
COPY arena_runner ./arena_runner
ENV ARENA_RUNNER_DB=/var/lib/arena-runner/state.db
EXPOSE 8080
# Runs as root only inside this container to talk to the socket; consider
# rootless Docker on the runner host for an additional boundary.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "60", "arena_runner.app:wsgi()"]

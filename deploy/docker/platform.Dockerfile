# CTFd + Arena platform image (Profiles A/B/C).
#
# Same build as the upstream CTFd Dockerfile, plus the Arena entrypoint that
# runs native AND Arena migrations before gunicorn starts. Build context is the
# repository root:  docker build -f deploy/docker/platform.Dockerfile .
# The matching platform.Dockerfile.dockerignore keeps secrets, virtualenvs,
# tests and the runner service out of the image.
FROM python:3.11-slim-bookworm AS build
WORKDIR /opt/CTFd
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libffi-dev libssl-dev git \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt /opt/CTFd/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY CTFd/plugins /opt/CTFd/CTFd/plugins
RUN for d in CTFd/plugins/*; do \
        if [ -f "$d/requirements.txt" ]; then pip install --no-cache-dir -r "$d/requirements.txt"; fi; \
    done

FROM python:3.11-slim-bookworm AS release
WORKDIR /opt/CTFd
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends libffi8 libssl3 \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/python -m pip uninstall -y pip \
    && useradd --no-log-init --shell /bin/bash -u 1001 ctfd \
    && mkdir -p /var/log/CTFd /var/uploads \
    && chown -R 1001:1001 /var/log/CTFd /var/uploads
COPY --chown=1001:1001 . /opt/CTFd
COPY --chown=1001:1001 --from=build /opt/venv /opt/venv
RUN chmod +x /opt/CTFd/docker-entrypoint.sh /opt/CTFd/deploy/docker/arena-entrypoint.sh
# The repo's .flaskenv sets FLASK_ENV=development for local `flask run`; the
# flask CLI would load it for the worker and migrations (debug mode, and with
# it unbounded SQLAlchemy query recording). Real env vars win over .flaskenv.
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 FLASK_ENV=production FLASK_DEBUG=0
USER 1001
EXPOSE 8000
ENTRYPOINT ["/opt/CTFd/deploy/docker/arena-entrypoint.sh"]
CMD ["web"]

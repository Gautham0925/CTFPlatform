#!/bin/bash
# Arena platform entrypoint.
#   web     : wait for DB, run native + Arena migrations, start gunicorn
#             (delegates to the upstream docker-entrypoint.sh).
#   worker  : wait for the Arena schema, then run the background worker.
#   migrate : run migrations only and exit (used by upgrade.sh).
#   other   : exec as a command (e.g. "flask arena check-integrity").
set -euo pipefail
cd /opt/CTFd
ROLE=${1:-web}

wait_db() {
  if [[ "${SKIP_DB_PING:-false}" == "false" ]]; then python ping.py; fi
}

migrate() {
  flask db upgrade
  flask arena migrate
}

case "$ROLE" in
  web)
    wait_db
    migrate
    # Migrations already ran; the upstream script re-runs the (idempotent)
    # native upgrade and then execs gunicorn with WORKERS/WORKER_CLASS.
    export SKIP_DB_PING=true
    exec /opt/CTFd/docker-entrypoint.sh
    ;;
  worker)
    wait_db
    # Never migrate from a worker: wait until the web container has done it.
    for _ in $(seq 1 120); do
      if flask arena db-current 2>/dev/null | tail -1 | grep -q '"up_to_date": true'; then
        break
      fi
      echo "worker: waiting for Arena schema..."; sleep 5
    done
    shift || true
    exec flask arena worker "$@"
    ;;
  migrate)
    wait_db
    migrate
    ;;
  *)
    exec "$@"
    ;;
esac

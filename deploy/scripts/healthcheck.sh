#!/usr/bin/env bash
# Operator health summary: container health, readiness, workers, job queue.
# Exit 0 only if everything is healthy. Safe to run from cron/monitoring.
source "$(dirname "$0")/lib.sh"
rc=0
if [[ "$MODE" == compose ]]; then
  for i in $(seq 1 ${ARENA_HEALTH_WAIT:-30}); do
    bad=$(compose ps --format '{{.Service}} {{.Health}}' | awk '$2!="healthy" && $2!=""')
    [[ -z "$bad" ]] && break
    sleep 5
  done
  compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Health}}'
  [[ -z "$bad" ]] || { log "unhealthy: $bad"; rc=1; }
  ready=$(compose exec -T ctfd python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/arena/readyz',timeout=5).read().decode())" 2>/dev/null || true)
else
  ready=$(curl -fsS "${ARENA_URL:-http://127.0.0.1:8000}/arena/readyz" || true)
fi
echo "readyz: ${ready:-unreachable}"
[[ "$ready" == *'"ready":true'* ]] || rc=1
if flask_cmd arena worker-health --max-age 90 >/dev/null 2>&1; then echo "workers: ok"; else echo "workers: NONE ALIVE"; rc=1; fi
echo "jobs: $(flask_cmd arena jobs stats 2>/dev/null | python3 -c "import json,sys; t=sys.stdin.read(); print(json.dumps(json.loads(t[t.index('{'):])))" 2>/dev/null || echo unavailable)"
exit $rc

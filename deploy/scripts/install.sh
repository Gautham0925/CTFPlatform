#!/usr/bin/env bash
# First-time install of Profile B (add the competition overlay for Profile C):
#   deploy/scripts/install.sh                       # Profile B
#   ARENA_COMPOSE_FILES=deploy/compose/community.yml:deploy/compose/competition.yml \
#     deploy/scripts/install.sh                     # Profile C
source "$(dirname "$0")/lib.sh"
[[ "$MODE" == compose ]] || die "install.sh is for the compose profiles; see INSTALLATION_GUIDE for bare metal"
command -v docker >/dev/null || die "docker is required"
docker compose version >/dev/null || die "docker compose v2 is required"
env_file=${ARENA_ENV_FILE:-$ROOT/deploy/.env}
if [[ ! -f "$env_file" ]]; then
  cp "$ROOT/deploy/examples/platform.env.example" "$env_file"
  "$ROOT/deploy/scripts/gen-secrets.sh" "$env_file"
  log "created $env_file - set ARENA_DOMAIN / ARENA_PUBLIC_URL, then re-run"
  exit 2
fi
grep -q '=CHANGE_ME' "$env_file" && die "$env_file still contains CHANGE_ME values (run gen-secrets.sh)"
[[ -s "$ROOT/deploy/nginx/certs/fullchain.pem" && -s "$ROOT/deploy/nginx/certs/privkey.pem" ]] \
  || die "TLS certificate missing in deploy/nginx/certs (see DEPLOYMENT_GUIDE; selfsigned-cert.sh for tests)"
mkdir -p "$ROOT/deploy/backups" "$ROOT/deploy/compose/runner-ca"
compose config --quiet
log "building image"
compose build migrate
log "starting stack (migrations run in the one-shot 'migrate' service)"
compose up -d
"$ROOT/deploy/scripts/healthcheck.sh"
log "done. Create the first admin by opening https://<domain>/setup"

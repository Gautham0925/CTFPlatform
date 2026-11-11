# Shared helpers for deploy scripts (sourced, not executed).
# Modes:
#   ARENA_DEPLOY_MODE=compose (default): talk to the compose stack.
#     ARENA_COMPOSE_FILES="deploy/compose/community.yml[:deploy/compose/competition.yml]"
#   ARENA_DEPLOY_MODE=direct: bare-metal install; uses DB_HOST, DB_PORT,
#     DB_USER, DB_PASSWORD, DB_NAME, UPLOAD_DIR and runs `flask` from CTFD_DIR.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODE=${ARENA_DEPLOY_MODE:-compose}
log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

compose() {
  local files=${ARENA_COMPOSE_FILES:-$ROOT/deploy/compose/community.yml} args=()
  IFS=':' read -ra fs <<< "$files"
  for f in "${fs[@]}"; do args+=(-f "$f"); done
  docker compose --env-file "${ARENA_ENV_FILE:-$ROOT/deploy/.env}" "${args[@]}" "$@"
}

# mysql client binaries (MariaDB names first, MySQL fallback)
dump_bin() { command -v mariadb-dump || command -v mysqldump || die "mariadb-dump not found"; }
sql_bin() { command -v mariadb || command -v mysql || die "mariadb client not found"; }

direct_defaults_file() {
  # Pass the password via a temporary option file, never on the command line.
  local f; f=$(mktemp); chmod 600 "$f"
  printf '[client]\nhost=%s\nport=%s\nuser=%s\npassword=%s\n' \
    "${DB_HOST:-127.0.0.1}" "${DB_PORT:-3306}" "${DB_USER:?DB_USER}" "${DB_PASSWORD:?DB_PASSWORD}" > "$f"
  echo "$f"
}

flask_cmd() {
  if [[ "$MODE" == compose ]]; then
    compose run --rm --no-deps -T migrate flask "$@"
  else
    (cd "${CTFD_DIR:-$ROOT}" && "${FLASK:-flask}" "$@")
  fi
}

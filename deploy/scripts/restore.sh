#!/usr/bin/env bash
# Restore a backup made by backup.sh. DESTRUCTIVE: replaces the database and
# uploaded files. Stops web + worker first so nothing writes during restore,
# then runs migrations (for backups from older versions) and the integrity
# check before starting them again.
#   deploy/scripts/restore.sh <backup-dir> --yes
source "$(dirname "$0")/lib.sh"
src=${1:?usage: restore.sh <backup-dir> --yes}
[[ "${2:-}" == "--yes" ]] || die "refusing to restore without --yes (this replaces all data)"
[[ -f "$src/SHA256SUMS" ]] || die "$src is not a backup directory"
(cd "$src" && sha256sum -c --quiet SHA256SUMS) || die "checksum mismatch; backup corrupt"
log "checksums OK"

if [[ "$MODE" == compose ]]; then
  log "stopping web and worker"
  compose stop ctfd worker nginx >/dev/null
  log "restoring database"
  compose exec -T db sh -c 'exec mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" -e "DROP DATABASE IF EXISTS ctfd; CREATE DATABASE ctfd CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; GRANT ALL ON ctfd.* TO \"ctfd\"@\"%\";"'
  zcat "$src/db.sql.gz" | compose exec -T db sh -c 'exec mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" ctfd'
  log "restoring uploads"
  compose run --rm --no-deps -T --entrypoint sh migrate -c 'find /var/uploads -mindepth 1 -delete && tar -C /var/uploads -xf -' \
    < <(zcat "$src/uploads.tar.gz")
  log "migrating + integrity check"
  compose run --rm --no-deps -T migrate migrate
  compose run --rm --no-deps -T migrate flask arena check-integrity
  log "starting services"
  compose up -d ctfd worker nginx >/dev/null
else
  cnf=$(direct_defaults_file); trap 'rm -f "$cnf"' EXIT
  db=${DB_NAME:?DB_NAME}
  [[ "$db" =~ ^[A-Za-z0-9_-]+$ ]] || die "invalid DB_NAME"
  log "restoring database $db (stop the web and worker processes first)"
  "$(sql_bin)" --defaults-extra-file="$cnf" -e "DROP DATABASE IF EXISTS \`$db\`; CREATE DATABASE \`$db\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
  zcat "$src/db.sql.gz" | "$(sql_bin)" --defaults-extra-file="$cnf" "$db"
  log "restoring uploads"
  up=${UPLOAD_DIR:?UPLOAD_DIR}
  mkdir -p "$up" && find "$up" -mindepth 1 -delete && zcat "$src/uploads.tar.gz" | tar -C "$up" -xf -
  log "migrating + integrity check"
  flask_cmd db upgrade && flask_cmd arena migrate && flask_cmd arena check-integrity
fi
log "restore complete from $src"

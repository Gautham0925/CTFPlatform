#!/usr/bin/env bash
# Consistent backup of the Arena platform: database (single transaction),
# uploaded files, and a manifest with schema revisions + checksums.
# Redis is NOT backed up: it holds only cache/lock/buffer data.
#   deploy/scripts/backup.sh [output-dir]      (default deploy/backups)
# Env: ARENA_BACKUP_KEEP=14 (number of backups to keep), plus lib.sh modes.
source "$(dirname "$0")/lib.sh"
out_root=${1:-$ROOT/deploy/backups}
ts=$(date -u +%Y%m%dT%H%M%SZ)
dest="$out_root/arena-$ts"
umask 077
mkdir -p "$dest"
DUMP_ARGS=(--single-transaction --quick --routines --triggers --events --hex-blob
           --default-character-set=utf8mb4 --no-tablespaces)

log "dumping database ($MODE)"
if [[ "$MODE" == compose ]]; then
  compose exec -T db sh -c 'exec mariadb-dump -uroot -p"$MARIADB_ROOT_PASSWORD" "$@" ctfd' _ "${DUMP_ARGS[@]}" \
    | gzip -c > "$dest/db.sql.gz"
else
  cnf=$(direct_defaults_file); trap 'rm -f "$cnf"' EXIT
  "$(dump_bin)" --defaults-extra-file="$cnf" "${DUMP_ARGS[@]}" "${DB_NAME:?DB_NAME}" | gzip -c > "$dest/db.sql.gz"
fi
# A failed dump inside a pipe must not produce a "successful" backup.
gzip -t "$dest/db.sql.gz"
zcat "$dest/db.sql.gz" | tail -n 1 | grep -q "Dump completed" || die "database dump incomplete"

log "archiving uploads"
if [[ "$MODE" == compose ]]; then
  compose exec -T ctfd tar -C /var/uploads -cf - . | gzip -c > "$dest/uploads.tar.gz"
else
  tar -C "${UPLOAD_DIR:?UPLOAD_DIR}" -cf - . | gzip -c > "$dest/uploads.tar.gz"
fi
gzip -t "$dest/uploads.tar.gz"

log "writing manifest"
arena_rev=$(flask_cmd arena db-current 2>/dev/null | tail -1 || echo '{}')
git_rev=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)
printf '{"created_utc":"%s","mode":"%s","git":"%s","arena":%s}\n' \
  "$ts" "$MODE" "$git_rev" "${arena_rev:-{\}}" > "$dest/manifest.json"
(cd "$dest" && sha256sum db.sql.gz uploads.tar.gz manifest.json > SHA256SUMS)

keep=${ARENA_BACKUP_KEEP:-14}
mapfile -t old < <(ls -1d "$out_root"/arena-* 2>/dev/null | sort | head -n -"$keep")
for d in "${old[@]}"; do log "pruning $d"; rm -rf -- "$d"; done
log "backup complete: $dest"
echo "$dest"

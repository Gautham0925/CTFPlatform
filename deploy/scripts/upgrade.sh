#!/usr/bin/env bash
# Upgrade: backup -> build new image -> stop worker -> migrate -> integrity
# check -> restart -> health check. Aborts (leaving the old containers
# running) if the backup or the build fails.
source "$(dirname "$0")/lib.sh"
[[ "$MODE" == compose ]] || die "see MIGRATION_GUIDE for bare-metal upgrades"
log "1/6 backup"
backup_dir=$("$ROOT/deploy/scripts/backup.sh")
log "2/6 build"
compose build migrate
log "3/6 stop workers (jobs are leased; unfinished ones are retried)"
compose stop worker
log "4/6 migrate"
compose run --rm --no-deps -T migrate migrate
log "5/6 integrity"
if ! compose run --rm --no-deps -T migrate flask arena check-integrity; then
  log "integrity check FAILED. Restore with: deploy/scripts/restore.sh $backup_dir --yes"
  exit 1
fi
log "6/6 restart"
compose up -d --remove-orphans
"$ROOT/deploy/scripts/healthcheck.sh"
log "upgrade complete (backup: $backup_dir)"

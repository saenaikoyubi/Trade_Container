#!/bin/sh
set -eu

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$repository_root"
env_file="${1:-.env.local}"

compose() {
  docker compose --env-file "$env_file" -f docker/compose.yaml "$@"
}

restore_created=false
cleanup() {
  if [ "$restore_created" = true ]; then
    compose --profile restore stop postgres-restore >/dev/null 2>&1 || true
    compose --profile restore rm -f postgres-restore >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

backup_output="$(compose --profile tools run --rm db-backup)"
printf '%s\n' "$backup_output"
backup_name="$(printf '%s\n' "$backup_output" |
  sed -n 's#^backup created: /backup/\(trade_[A-Za-z0-9_]*\.dump\)$#\1#p' |
  tail -n 1)"
if [ -z "$backup_name" ]; then
  echo "could not identify the newly created backup artifact" >&2
  exit 1
fi
backup_path="docker/db-backup/volume/output/$backup_name"
if [ ! -s "$backup_path" ]; then
  echo "backup artifact is missing: $backup_path" >&2
  exit 1
fi

if [ -n "$(compose --profile restore ps -q postgres-restore)" ]; then
  echo "an isolated restore is already running; retry when it finishes" >&2
  exit 1
fi
compose --profile restore rm -f postgres-restore >/dev/null

BACKUP_FILE="$backup_name"
RESTORE_CONFIRM=isolated
export BACKUP_FILE RESTORE_CONFIRM
restore_created=true
compose --profile restore up -d postgres-restore
restore_output="$(compose --profile restore run --rm db-restore)"
printf '%s\n' "$restore_output"
if ! printf '%s\n' "$restore_output" | grep -q 'restore verification passed:'; then
  echo "restore completed without a verification result" >&2
  exit 1
fi

dump_sha256="$(sha256sum "$backup_path" | awk '{print $1}')"
metadata_path="${backup_path%.dump}.metadata"
recorded_sha256="$(sed -n 's/^dump_sha256=//p' "$metadata_path")"
if [ "$recorded_sha256" != "$dump_sha256" ]; then
  echo "backup archive changed after restore verification" >&2
  exit 1
fi
{
  printf 'format=trade-container-verified-v1\n'
  printf 'dump_sha256=%s\n' "$dump_sha256"
  printf 'verified_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$backup_path.verified"
printf 'verified backup: %s\n' "$backup_path"

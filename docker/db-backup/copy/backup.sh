#!/bin/sh
set -eu

umask 077

password_file="${DATABASE_PASSWORD_FILE:-/run/secrets/postgres_password}"
if [ ! -s "$password_file" ]; then
  echo "database password file is missing or empty: $password_file" >&2
  exit 1
fi

export PGPASSWORD="$(cat "$password_file")"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="trade_${timestamp}.dump"
output="/backup/$name"
checksum_output="$output.sha256"
metadata_output="/backup/trade_${timestamp}.metadata"
temporary_dump="/backup/.${name}.tmp"
temporary_checksum="/backup/.${name}.sha256.tmp"
temporary_metadata="/backup/.trade_${timestamp}.metadata.tmp"

cleanup() {
  rm -f "$temporary_dump" "$temporary_checksum" "$temporary_metadata"
}
trap cleanup EXIT INT TERM

sql() {
  psql \
    --host="${POSTGRES_HOST:-postgres}" \
    --port="${POSTGRES_PORT:-5432}" \
    --username="${POSTGRES_USER:-trade}" \
    --dbname="${POSTGRES_DB:-trade}" \
    --no-align --tuples-only --quiet \
    --command="$1"
}

state_query="SELECT concat_ws('|',
  (SELECT string_agg(version, ',' ORDER BY version) FROM schema_migrations),
  (SELECT count(*) FROM orders),
  (SELECT count(*) FROM fills),
  (SELECT count(*) FROM positions),
  (SELECT count(*) FROM daily_pnl)
)"
state_before="$(sql "$state_query")"

pg_dump \
  --host="${POSTGRES_HOST:-postgres}" \
  --port="${POSTGRES_PORT:-5432}" \
  --username="${POSTGRES_USER:-trade}" \
  --dbname="${POSTGRES_DB:-trade}" \
  --format=custom \
  --serializable-deferrable \
  --file="$temporary_dump"

state_after="$(sql "$state_query")"
if [ "$state_before" != "$state_after" ]; then
  echo "tracked database state changed during backup; retry" >&2
  exit 1
fi

pg_restore --list "$temporary_dump" >/dev/null
dump_sha256="$(sha256sum "$temporary_dump" | awk '{print $1}')"
printf '%s  %s\n' "$dump_sha256" "$name" >"$temporary_checksum"

schema_migrations="$(printf '%s' "$state_after" | cut -d'|' -f1)"
orders="$(printf '%s' "$state_after" | cut -d'|' -f2)"
fills="$(printf '%s' "$state_after" | cut -d'|' -f3)"
positions="$(printf '%s' "$state_after" | cut -d'|' -f4)"
daily_pnl="$(printf '%s' "$state_after" | cut -d'|' -f5)"
server_version="$(sql 'SHOW server_version')"

cat >"$temporary_metadata" <<EOF
format=trade-container-backup-v1
created_at_utc=${timestamp}
database=${POSTGRES_DB:-trade}
postgres_server_version=${server_version}
dump_file=${name}
dump_sha256=${dump_sha256}
schema_migrations=${schema_migrations}
orders=${orders}
fills=${fills}
positions=${positions}
daily_pnl=${daily_pnl}
EOF

mv "$temporary_checksum" "$checksum_output"
mv "$temporary_metadata" "$metadata_output"
mv "$temporary_dump" "$output"
trap - EXIT INT TERM

echo "backup created: $output"
echo "checksum created: $checksum_output"
echo "metadata created: $metadata_output"

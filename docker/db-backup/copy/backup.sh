#!/bin/sh
set -eu

umask 077

password_file="${DATABASE_PASSWORD_FILE:-/run/secrets/postgres_password}"
if [ ! -s "$password_file" ]; then
  echo "database password file is missing or empty: $password_file" >&2
  exit 1
fi

export PGPASSWORD="$(cat "$password_file")"
backup_dir="${BACKUP_DIR:-/backup}"
mkdir -p "$backup_dir"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
unique="$(od -An -N16 -tx1 /dev/urandom | tr -d '[:space:]')"
name="trade_${timestamp}_${unique}.dump"
output="$backup_dir/$name"
checksum_output="$output.sha256"
metadata_output="$backup_dir/trade_${timestamp}_${unique}.metadata"
temporary_dump="$backup_dir/.${name}.tmp"
temporary_checksum="$backup_dir/.${name}.sha256.tmp"
temporary_metadata="$backup_dir/.trade_${timestamp}_${unique}.metadata.tmp"

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

tables="$(sql "SELECT string_agg(tablename, ',' ORDER BY tablename) FROM pg_tables WHERE schemaname = 'public'")"
if [ -z "$tables" ]; then
  echo "no public tables exist to back up" >&2
  exit 1
fi

table_counts() {
  old_ifs="$IFS"
  IFS=,
  for table in $tables; do
    case "$table" in
      ""|*[!A-Za-z0-9_]*) echo "invalid table name: $table" >&2; exit 1 ;;
    esac
    printf 'count_%s=%s\n' "$table" "$(sql "SELECT count(*) FROM public.$table")"
  done
  IFS="$old_ifs"
}

columns_query="SELECT string_agg(
  c.relname || '.' || a.attname || ':' || format_type(a.atttypid, a.atttypmod) || ':' || a.attnotnull || ':' || coalesce(pg_get_expr(d.adbin, d.adrelid), ''),
  E'\\n' ORDER BY c.relname, a.attnum)
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
  LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
  WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
constraints_query="SELECT string_agg(
  concat_ws(':', c.conrelid::regclass::text, c.conname, c.contype,
    coalesce(c.conkey::text, ''),
    CASE WHEN c.confrelid = 0 THEN '' ELSE c.confrelid::regclass::text END,
    coalesce(c.confkey::text, ''), c.convalidated::text,
    c.condeferrable::text, c.condeferred::text),
  E'\\n' ORDER BY c.conrelid::regclass::text, c.conname)
  FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
  WHERE n.nspname = 'public'"
state_before="$(table_counts)"

pg_dump \
  --host="${POSTGRES_HOST:-postgres}" \
  --port="${POSTGRES_PORT:-5432}" \
  --username="${POSTGRES_USER:-trade}" \
  --dbname="${POSTGRES_DB:-trade}" \
  --format=custom \
  --serializable-deferrable \
  --file="$temporary_dump"

state_after="$(table_counts)"
if [ "$state_before" != "$state_after" ]; then
  echo "tracked database state changed during backup; retry" >&2
  exit 1
fi

pg_restore --list "$temporary_dump" >/dev/null
dump_sha256="$(sha256sum "$temporary_dump" | awk '{print $1}')"
printf '%s  %s\n' "$dump_sha256" "$name" >"$temporary_checksum"

schema_migrations="$(sql "SELECT string_agg(version, ',' ORDER BY version) FROM schema_migrations")"
columns_sha256="$(sql "$columns_query" | sha256sum | awk '{print $1}')"
constraints_sha256="$(sql "$constraints_query" | sha256sum | awk '{print $1}')"
server_version="$(sql 'SHOW server_version')"

cat >"$temporary_metadata" <<EOF
format=trade-container-backup-v2
created_at_utc=${timestamp}
database=${POSTGRES_DB:-trade}
postgres_server_version=${server_version}
dump_file=${name}
dump_sha256=${dump_sha256}
schema_migrations=${schema_migrations}
tables=${tables}
columns_sha256=${columns_sha256}
constraints_sha256=${constraints_sha256}
EOF
printf '%s\n' "$state_after" >> "$temporary_metadata"

mv "$temporary_dump" "$output"
mv "$temporary_checksum" "$checksum_output"
mv "$temporary_metadata" "$metadata_output"
trap - EXIT INT TERM

echo "backup created: $output"
echo "checksum created: $checksum_output"
echo "metadata created: $metadata_output"

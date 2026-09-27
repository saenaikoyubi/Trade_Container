#!/bin/sh
set -eu

metadata_file="${METADATA_FILE:-}"
host="${POSTGRES_HOST:-postgres-restore}"
port="${POSTGRES_PORT:-5432}"
database="${POSTGRES_DB:-trade_restore}"
user="${POSTGRES_USER:-trade}"
backup_dir="${BACKUP_DIR:-/backup}"

if [ ! -r "$metadata_file" ]; then
  echo "restore metadata is missing: $metadata_file" >&2
  exit 1
fi

metadata_value() {
  value="$(sed -n "s/^$1=//p" "$metadata_file")"
  if [ -z "$value" ]; then
    echo "metadata key is missing: $1" >&2
    exit 1
  fi
  printf '%s' "$value"
}

if [ "$(metadata_value format)" != "trade-container-backup-v2" ]; then
  echo "unsupported backup metadata format" >&2
  exit 1
fi
if [ "$(metadata_value database)" != "${SOURCE_DATABASE_NAME:-trade}" ]; then
  echo "backup metadata is not for the trade database" >&2
  exit 1
fi

sql() {
  psql \
    --host="$host" \
    --port="$port" \
    --username="$user" \
    --dbname="$database" \
    --no-align --tuples-only --quiet \
    --command="$1"
}

archive="$backup_dir/$(metadata_value dump_file)"
expected_hash="$(metadata_value dump_sha256)"
actual_hash="$(sha256sum "$archive" | awk '{print $1}')"
if [ "$actual_hash" != "$expected_hash" ]; then
  echo "restored archive hash does not match metadata" >&2
  exit 1
fi

actual_migrations="$(sql "SELECT string_agg(version, ',' ORDER BY version) FROM schema_migrations")"
if [ "$actual_migrations" != "$(metadata_value schema_migrations)" ]; then
  echo "schema migration list does not match backup metadata" >&2
  exit 1
fi

expected_tables="$(metadata_value tables)"
actual_tables="$(sql "SELECT string_agg(tablename, ',' ORDER BY tablename) FROM pg_tables WHERE schemaname = 'public'")"
if [ "$actual_tables" != "$expected_tables" ]; then
  echo "restored table list does not match backup metadata" >&2
  exit 1
fi

old_ifs="$IFS"
IFS=,
for table in $expected_tables; do
  case "$table" in
    ""|*[!A-Za-z0-9_]*) echo "invalid table name in backup metadata" >&2; exit 1 ;;
  esac
  expected="$(metadata_value "count_$table")"
  actual="$(sql "SELECT count(*) FROM public.$table")"
  if [ "$actual" != "$expected" ]; then
    echo "$table count mismatch: expected=$expected actual=$actual" >&2
    exit 1
  fi
done
IFS="$old_ifs"

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

columns_sha256="$(sql "$columns_query" | sha256sum | awk '{print $1}')"
if [ "$columns_sha256" != "$(metadata_value columns_sha256)" ]; then
  echo "restored column definitions do not match backup metadata" >&2
  exit 1
fi

constraints_sha256="$(sql "$constraints_query" | sha256sum | awk '{print $1}')"
if [ "$constraints_sha256" != "$(metadata_value constraints_sha256)" ]; then
  echo "restored constraints do not match backup metadata" >&2
  exit 1
fi

echo "restore verification passed: archive=$(basename "$archive") database=$database"

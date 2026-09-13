#!/bin/sh
set -eu

metadata_file="${METADATA_FILE:-}"
host="${POSTGRES_HOST:-postgres-restore}"
port="${POSTGRES_PORT:-5432}"
database="${POSTGRES_DB:-trade_restore}"
user="${POSTGRES_USER:-trade}"

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

if [ "$(metadata_value format)" != "trade-container-backup-v1" ]; then
  echo "unsupported backup metadata format" >&2
  exit 1
fi
if [ "$(metadata_value database)" != "trade" ]; then
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

archive="/backup/$(metadata_value dump_file)"
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

for table in orders fills positions daily_pnl; do
  expected="$(metadata_value "$table")"
  actual="$(sql "SELECT count(*) FROM $table")"
  if [ "$actual" != "$expected" ]; then
    echo "$table count mismatch: expected=$expected actual=$actual" >&2
    exit 1
  fi
done

constraint_count="$(sql "SELECT count(*) FROM pg_constraint WHERE conname IN (
  'schema_migrations_pkey',
  'orders_pkey', 'orders_request_id_key',
  'fills_pkey', 'fills_order_id_fkey', 'uq_fill_order_sequence',
  'positions_pkey', 'uq_position_exchange_symbol',
  'daily_pnl_pkey', 'uq_daily_pnl_date'
)")"
if [ "$constraint_count" != "10" ]; then
  echo "required database constraints are missing" >&2
  exit 1
fi

column_count="$(sql "SELECT count(*) FROM information_schema.columns WHERE
  (table_name = 'orders' AND column_name IN (
    'resting_since', 'last_market_data_id', 'retry_count', 'next_attempt_at', 'strategy_id'
  )) OR
  (table_name = 'fills' AND column_name IN ('liquidity_role', 'market_data_id')) OR
  (table_name = 'schema_migrations' AND column_name = 'checksum')")"
if [ "$column_count" != "8" ]; then
  echo "required paper-execution columns are missing" >&2
  exit 1
fi

exchange_column_count="$(sql "SELECT count(*) FROM information_schema.columns WHERE
  (table_name = 'orders' AND column_name IN (
    'exchange_id', 'exchange_network', 'reduce_only'
  )) OR
  (table_name = 'fills' AND column_name = 'exchange_id') OR
  (table_name = 'positions' AND column_name = 'exchange_id') OR
  (table_name = 'control_flags' AND column_name = 'close_only')")"
if [ "$exchange_column_count" != "6" ]; then
  echo "required exchange-adapter columns are missing" >&2
  exit 1
fi

control_constraint_count="$(sql "SELECT count(*) FROM pg_constraint WHERE
  conname = 'ck_control_flags_exclusive_modes'")"
if [ "$control_constraint_count" != "1" ]; then
  echo "required trading-control constraint is missing" >&2
  exit 1
fi

echo "restore verification passed: archive=$(basename "$archive") database=$database"

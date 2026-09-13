#!/bin/sh
set -eu

backup_file="${BACKUP_FILE:-}"
confirm="${RESTORE_CONFIRM:-}"
host="${POSTGRES_HOST:-postgres-restore}"
port="${POSTGRES_PORT:-5432}"
database="${POSTGRES_DB:-trade_restore}"
user="${POSTGRES_USER:-trade}"
password_file="${DATABASE_PASSWORD_FILE:-/run/secrets/postgres_password}"

if [ "$confirm" != "isolated" ]; then
  echo "RESTORE_CONFIRM=isolated is required" >&2
  exit 1
fi
if [ "$host" != "postgres-restore" ] || [ "$database" != "trade_restore" ]; then
  echo "restore target must be postgres-restore/trade_restore" >&2
  exit 1
fi
case "$backup_file" in
  trade_*.dump) ;;
  *) echo "BACKUP_FILE must be a trade_*.dump basename" >&2; exit 1 ;;
esac
case "$backup_file" in
  */*|*\\*|*..*) echo "BACKUP_FILE must not contain a path" >&2; exit 1 ;;
esac
if [ ! -s "$password_file" ]; then
  echo "database password file is missing or empty: $password_file" >&2
  exit 1
fi

archive="/backup/$backup_file"
checksum_file="$archive.sha256"
metadata_file="/backup/${backup_file%.dump}.metadata"
for required in "$archive" "$checksum_file" "$metadata_file"; do
  if [ ! -r "$required" ]; then
    echo "required restore file is missing: $required" >&2
    exit 1
  fi
done

export PGPASSWORD="$(cat "$password_file")"
(cd /backup && sha256sum -c "$backup_file.sha256")
pg_restore --list "$archive" >/dev/null

attempt=1
while ! pg_isready --host="$host" --port="$port" --username="$user" --dbname="$database" >/dev/null 2>&1; do
  if [ "$attempt" -ge 30 ]; then
    echo "restore database did not become ready" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 1
done

table_count="$(psql --host="$host" --port="$port" --username="$user" --dbname="$database" --no-align --tuples-only --quiet --command="SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
if [ "$table_count" != "0" ]; then
  echo "restore target is not empty" >&2
  exit 1
fi

pg_restore \
  --host="$host" \
  --port="$port" \
  --username="$user" \
  --dbname="$database" \
  --exit-on-error \
  --single-transaction \
  --no-owner \
  --no-privileges \
  "$archive"

METADATA_FILE="$metadata_file" /usr/local/bin/verify-restore

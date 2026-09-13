from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import psycopg

from trade_common.config import read_secret


MIGRATION_LOCK_ID = 814_202_607_15


def file_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def connection_string() -> str:
    password = read_secret(os.getenv("DATABASE_PASSWORD_FILE", "/run/secrets/postgres_password"))
    return (
        f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'trade')} "
        f"user={os.getenv('POSTGRES_USER', 'trade')} password={password}"
    )


def connect_with_retry(attempts: int = 30, delay_seconds: float = 2.0):
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return psycopg.connect(connection_string())
        except psycopg.OperationalError as exc:
            last_error = exc
            print(f"database connection attempt {attempt}/{attempts} failed; retrying")
            time.sleep(delay_seconds)
    raise RuntimeError("database did not become available") from last_error


def main() -> None:
    migration_dir = Path(os.getenv("MIGRATION_DIR", "/app/migrations"))
    migrations = sorted(migration_dir.glob("*.sql"))
    if not migrations:
        raise RuntimeError(f"no migration files found in {migration_dir}")

    with connect_with_retry() as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    checksum TEXT,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            connection.execute("ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum TEXT")
            connection.commit()

            applied = {
                row[0]: row[1]
                for row in connection.execute("SELECT version, checksum FROM schema_migrations")
            }
            connection.commit()
            for migration in migrations:
                checksum = file_checksum(migration)
                if migration.name in applied:
                    recorded = applied[migration.name]
                    if recorded is None:
                        connection.execute(
                            "UPDATE schema_migrations SET checksum = %s WHERE version = %s",
                            (checksum, migration.name),
                        )
                        connection.commit()
                        print(f"baseline checksum {migration.name}")
                    elif recorded != checksum:
                        raise RuntimeError(f"applied migration was modified: {migration.name}")
                    else:
                        print(f"skip {migration.name}")
                    continue

                print(f"apply {migration.name}")
                with connection.transaction():
                    connection.execute(migration.read_text(encoding="utf-8"))
                    connection.execute(
                        "INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)",
                        (migration.name, checksum),
                    )
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))
            connection.commit()


if __name__ == "__main__":
    main()

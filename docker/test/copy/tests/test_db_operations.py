import subprocess

from db_migrate import file_checksum


def test_migration_checksum_detects_content_change(tmp_path):
    migration = tmp_path / "001_test.sql"
    migration.write_text("SELECT 1;", encoding="utf-8")
    first = file_checksum(migration)
    migration.write_text("SELECT 2;", encoding="utf-8")

    assert file_checksum(migration) != first


def test_database_shell_scripts_have_valid_syntax():
    for script in ("backup.sh", "restore.sh", "verify.sh"):
        subprocess.run(["sh", "-n", f"/app/db_scripts/{script}"], check=True)

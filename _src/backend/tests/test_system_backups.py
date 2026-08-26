from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.database import Database
from app.main import scan_admin_system_backups
from app.system_backups import (
    SystemBackupError,
    create_archive,
    extract_archive,
    next_cron,
    snapshot_sqlite,
    unwrap_passphrase,
    wrap_passphrase,
)


def test_encrypted_archive_round_trip_and_wrong_password(tmp_path: Path) -> None:
    archive = tmp_path / "savedstream-system-20260825-120000.ssbak"
    manifest = create_archive(
        archive,
        passphrase="correct horse battery staple",
        sections={"savedstream.db": b"sqlite", "runtime_config.json": b"{}"},
    )
    assert manifest["format_version"] == 1
    output = tmp_path / "output"
    extract_archive(archive, "correct horse battery staple", output)
    assert (output / "savedstream.db").read_bytes() == b"sqlite"
    with pytest.raises(SystemBackupError):
        extract_archive(archive, "wrong password", tmp_path / "wrong")


def test_passphrase_wrapping_does_not_store_plaintext() -> None:
    wrapped = wrap_passphrase("correct horse battery staple", "admin-secret")
    assert "correct horse" not in json.dumps(wrapped)
    assert unwrap_passphrase(wrapped, "admin-secret") == "correct horse battery staple"
    with pytest.raises(SystemBackupError):
        unwrap_passphrase(wrapped, "wrong-admin-secret")


def test_cron_timezone_and_database_ephemeral_session_removal(tmp_path: Path) -> None:
    value = next_cron("0 3 * * *", "Asia/Shanghai")
    assert value.tzinfo is not None


@pytest.mark.asyncio
async def test_system_backup_database_tables_and_settings(tmp_path: Path) -> None:
    database = Database(tmp_path / "savedstream.db")
    await database.initialize()
    settings = await database.get_system_backup_settings()
    assert settings["cron_expr"] == "0 3 * * *"
    await database.update_system_backup_settings({"enabled": 1, "timezone": "Asia/Shanghai"})
    updated = await database.get_system_backup_settings()
    assert updated["enabled"] == 1
    assert updated["timezone"] == "Asia/Shanghai"
    job = await database.create_system_backup_job({
        "id": "job-1", "backup_id": None, "trigger": "manual", "status": "queued", "phase": "queued",
        "progress": 0, "attempts": 0, "temp_path": None, "error": None, "created_by": None,
        "created_at": "2026-08-25T00:00:00+00:00", "updated_at": "2026-08-25T00:00:00+00:00", "completed_at": None,
    })
    assert job["id"] == "job-1"


@pytest.mark.asyncio
async def test_system_backup_telegram_location_upsert_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "savedstream.db")
    await database.initialize()
    original = await database.create_system_backup({
        "id": "local-random-id",
        "filename": "savedstream-system-20260826-120000.ssbak",
        "source": "manual",
        "status": "available",
        "created_at": "2026-08-26T04:00:00+00:00",
        "size_bytes": 123,
        "sha256": "known-sha256",
        "account_id": "alpha",
        "message_id": 77,
        "manifest_json": '{"format_version":1}',
        "error": None,
        "imported_at": "2026-08-26T05:00:00+00:00",
    })
    rescanned = await database.create_system_backup({
        "id": "telegram-derived-id",
        "filename": original["filename"],
        "source": "telegram",
        "status": "available",
        "created_at": original["created_at"],
        "size_bytes": 123,
        "sha256": "",
        "account_id": "alpha",
        "message_id": 77,
        "manifest_json": '{"marker":"#savedstream-system-backup:v1"}',
        "error": None,
        "imported_at": None,
    })

    assert rescanned["id"] == "local-random-id"
    assert rescanned["sha256"] == "known-sha256"
    assert rescanned["imported_at"] == "2026-08-26T05:00:00+00:00"
    assert len(await database.list_system_backups()) == 1


@pytest.mark.asyncio
async def test_scan_telegram_reuses_existing_backup_and_configured_account(tmp_path: Path) -> None:
    class FakeTeleBox:
        requested_account = ""

        async def resolve_account(self, account_id: str) -> str:
            self.requested_account = account_id
            return account_id

        async def list_system_backups(self, *, account_id: str) -> list[dict]:
            assert account_id == "configured-backup"
            return [{
                "id": 88,
                "filename": "savedstream-system-20260826-130000.ssbak",
                "date": "2026-08-26T05:00:00+00:00",
                "size": 456,
            }]

    database = Database(tmp_path / "savedstream.db")
    await database.initialize()
    await database.update_system_backup_settings({"account_id": "configured-backup"})
    await database.create_system_backup({
        "id": "existing-local-id",
        "filename": "savedstream-system-20260826-130000.ssbak",
        "source": "scheduled",
        "status": "available",
        "created_at": "2026-08-26T05:00:00+00:00",
        "size_bytes": 456,
        "sha256": "archive-sha",
        "account_id": "configured-backup",
        "message_id": 88,
        "manifest_json": '{"format_version":1}',
        "error": None,
        "imported_at": None,
    })
    telegram = FakeTeleBox()

    result = await scan_admin_system_backups(account_id="", database=database, telegram=telegram)  # type: ignore[arg-type]

    assert telegram.requested_account == "configured-backup"
    assert result["discovered"] == 0
    assert len(result["items"]) == 1
    assert result["items"][0]["id"] == "existing-local-id"
    manifest = json.loads(result["items"][0]["manifest_json"])
    assert manifest["format_version"] == 1
    assert manifest["telegram"]["id"] == 88


@pytest.mark.asyncio
async def test_indexer_marks_backup_messages_hidden(tmp_path: Path) -> None:
    database = Database(tmp_path / "savedstream.db")
    await database.initialize()
    item = await database.upsert_media_index({
        "account_id": "default", "id": 99, "kind": "file", "mime_type": "application/x-savedstream-backup",
        "size": 10, "filename": "savedstream-system-20260825-120000.ssbak", "original_title": "backup",
        "caption": "#savedstream-system-backup:v1", "date": "2026-08-25T00:00:00+00:00",
    })
    assert item["hidden"] == 1
    import aiosqlite
    async with aiosqlite.connect(database.path) as connection:
        row = await (await connection.execute("SELECT upload_source FROM media_index WHERE account_id='default' AND message_id=99")).fetchone()
    assert row and row[0] == "system_backup"


def test_sqlite_snapshot_excludes_live_sessions(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    database = Database(source)
    import asyncio

    asyncio.run(database.initialize())
    snapshot = tmp_path / "snapshot.db"
    snapshot_sqlite(source, snapshot)
    import sqlite3

    with sqlite3.connect(snapshot) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "auth_sessions" in tables
        assert connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0

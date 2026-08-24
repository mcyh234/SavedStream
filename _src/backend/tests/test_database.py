from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from app.database import DEFAULT_CACHE_BYTES, Database


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "nested" / "savedstream.db")
    await instance.initialize()
    return instance


@pytest.mark.asyncio
async def test_initialize_creates_defaults(database: Database) -> None:
    assert database.path.is_file()
    assert await database.get_cache_limit() == DEFAULT_CACHE_BYTES
    assert not await database.access_restricted()
    assert await database.get_setting("viewer_key_hash") == ""
    assert await database.get_setting("missing", "fallback") == "fallback"


@pytest.mark.asyncio
async def test_settings_are_upserted_and_cache_limit_is_sanitized(database: Database) -> None:
    await database.set_setting("access_restricted", "1")
    assert await database.access_restricted()
    await database.set_setting("cache_max_bytes", str(1024**3))
    assert await database.get_cache_limit() == 1024**3
    await database.set_setting("cache_max_bytes", "1")
    assert await database.get_cache_limit() == 512 * 1024 * 1024
    await database.set_setting("cache_max_bytes", "invalid")
    assert await database.get_cache_limit() == DEFAULT_CACHE_BYTES


@pytest.mark.asyncio
async def test_access_settings_are_updated_together(database: Database) -> None:
    await database.update_access_settings(cache_max_bytes=3 * 1024**3, access_restricted=True, viewer_key_hash="hashed-viewer-key")
    assert await database.get_cache_limit() == 3 * 1024**3
    assert await database.access_restricted()
    assert await database.get_setting("viewer_key_hash") == "hashed-viewer-key"


@pytest.mark.asyncio
async def test_local_titles_can_be_created_updated_and_removed(database: Database) -> None:
    assert await database.get_local_titles([]) == {}
    await database.set_local_title(101, "  First title  ")
    await database.set_local_title(202, "Second title")
    assert await database.get_local_titles([101, 202, 303]) == {101: "First title", 202: "Second title"}
    await database.set_local_title(101, "Updated")
    assert await database.get_local_titles([101]) == {101: "Updated"}
    await database.set_local_title(101, "   ")
    assert await database.get_local_titles([101, 202]) == {202: "Second title"}


def test_local_titles_are_isolated_by_account(tmp_path: Path) -> None:
    async def verify() -> None:
        database = Database(tmp_path / "accounts.db")
        await database.initialize()
        await database.set_local_title(42, "Account A", "alpha")
        await database.set_local_title(42, "Account B", "beta")
        assert await database.get_local_titles([42], "alpha") == {42: "Account A"}
        assert await database.get_local_titles([42], "beta") == {42: "Account B"}

    asyncio.run(verify())


def test_device_keys_can_be_registered_touched_and_revoked(tmp_path: Path) -> None:
    async def verify() -> None:
        database = Database(tmp_path / "device-keys.db")
        await database.initialize()
        await database.register_device_key("fingerprint", "public-key")
        record = await database.get_device_key("fingerprint")
        assert record is not None
        assert record["public_key_pem"] == "public-key"
        assert record["revoked"] == 0
        await database.revoke_device_key("fingerprint")
        assert (await database.get_device_key("fingerprint"))["revoked"] == 1
        assert not await database.register_device_key("fingerprint", "updated-key")
        restored = await database.get_device_key("fingerprint")
        assert restored["public_key_pem"] == "updated-key"
        assert restored["revoked"] == 1

    asyncio.run(verify())


def test_initialize_migrates_legacy_titles_and_backfills_search_index(tmp_path: Path) -> None:
    async def verify() -> None:
        path = tmp_path / "legacy.db"
        async with aiosqlite.connect(path) as connection:
            await connection.execute(
                "CREATE TABLE media_metadata(message_id INTEGER PRIMARY KEY, local_title TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            await connection.execute(
                "INSERT INTO media_metadata VALUES(?,?,?)",
                (42, "Legacy title", "2026-08-01T00:00:00+00:00"),
            )
            await connection.commit()

        database = Database(path)
        await database.initialize()
        assert await database.get_local_titles([42]) == {42: "Legacy title"}

        await database.upsert_media_index(
            {
                "account_id": "alpha",
                "id": 9,
                "kind": "image",
                "mime_type": "image/jpeg",
                "size": 10,
                "filename": "poster.jpg",
                "original_title": "A searchable title",
                "caption": "caption",
                "date": "2026-08-01T00:00:00+00:00",
            }
        )
        async with aiosqlite.connect(path) as connection:
            await connection.execute(
                "DELETE FROM media_index_fts WHERE account_id=? AND message_id=?",
                ("alpha", 9),
            )
            await connection.commit()
        await database.initialize()
        items, _, _ = await database.list_media_index(
            account_id="alpha",
            limit=10,
            cursor=None,
            order="newest",
            kind="all",
            query="searchable",
            visibility="private",
        )
        assert [item["id"] for item in items] == [9]

    asyncio.run(verify())


def test_initialize_adds_ingest_provenance_without_rebuilding_existing_media(tmp_path: Path) -> None:
    async def verify() -> None:
        path = tmp_path / "pre-ingest-provenance.db"
        async with aiosqlite.connect(path) as connection:
            await connection.execute(
                """
                CREATE TABLE media_index (
                    account_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    original_title TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    message_date TEXT NOT NULL,
                    date_year INTEGER NOT NULL,
                    date_month TEXT NOT NULL,
                    date_day TEXT NOT NULL,
                    duration REAL,
                    width INTEGER,
                    height INTEGER,
                    has_thumbnail INTEGER NOT NULL DEFAULT 0,
                    visibility TEXT NOT NULL DEFAULT 'private',
                    deleted INTEGER NOT NULL DEFAULT 0,
                    indexed_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, message_id)
                )
                """
            )
            await connection.execute(
                "INSERT INTO media_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "alpha",
                    42,
                    "image",
                    "image/jpeg",
                    100,
                    "legacy.jpg",
                    "Legacy",
                    "",
                    "2026-08-01T00:00:00+00:00",
                    2026,
                    "2026-08",
                    "2026-08-01",
                    None,
                    None,
                    None,
                    1,
                    "private",
                    0,
                    "2026-08-01T00:00:00+00:00",
                    "2026-08-01T00:00:00+00:00",
                ),
            )
            await connection.commit()

        database = Database(path)
        await database.initialize()

        async with aiosqlite.connect(path) as connection:
            columns = {
                row[1]
                for row in await (await connection.execute("PRAGMA table_info(media_index)")).fetchall()
            }
        assert {"source_ingest_job_id", "submitter_telegram_user_id"}.issubset(columns)
        preserved = await database.get_media_index("alpha", 42)
        assert preserved and preserved["title"] == "Legacy"

    asyncio.run(verify())


def test_upload_cancellation_is_atomic_and_terminal_jobs_are_preserved(tmp_path: Path) -> None:
    async def verify() -> None:
        database = Database(tmp_path / "uploads.db")
        await database.initialize()
        await database.create_upload_job(
            job_id="pending",
            account_id="alpha",
            filename="movie.mp4",
            mime_type="video/mp4",
            size=10,
            temp_path=str(tmp_path / "movie.upload"),
        )
        cancelled = await database.cancel_upload_job("pending")
        assert cancelled and cancelled["status"] == "cancelled"
        assert await database.complete_upload_job("pending", message_id=99)
        assert (await database.get_upload_job("pending"))["status"] == "cancelled"

        await database.create_upload_job(
            job_id="completed",
            account_id="alpha",
            filename="done.mp4",
            mime_type="video/mp4",
            size=10,
            temp_path=str(tmp_path / "done.upload"),
        )
        await database.complete_upload_job("completed", message_id=100)
        preserved = await database.cancel_upload_job("completed")
        assert preserved and preserved["status"] == "completed"

    asyncio.run(verify())

from __future__ import annotations

import asyncio
from pathlib import Path

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

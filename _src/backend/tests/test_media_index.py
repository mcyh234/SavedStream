from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from app.database import Database


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "media-index.db")
    await instance.initialize()
    return instance


def media(account: str, message_id: int, date: str, title: str = "title") -> dict:
    return {
        "account_id": account,
        "id": message_id,
        "kind": "image",
        "mime_type": "image/jpeg",
        "size": 100,
        "filename": f"{title}.jpg",
        "original_title": title,
        "caption": title,
        "date": date,
        "has_thumbnail": True,
    }


@pytest.mark.asyncio
async def test_media_index_is_account_isolated_and_grouped_by_day(database: Database) -> None:
    await database.upsert_media_index(media("alpha", 42, "2026-08-01T10:00:00+00:00", "alpha"))
    await database.upsert_media_index(media("beta", 42, "2026-08-02T10:00:00+00:00", "beta"))
    items, cursor, has_more = await database.list_media_index(
        account_id="alpha", limit=10, cursor=None, order="newest", kind="all", query="", visibility="private"
    )
    assert [item["id"] for item in items] == [42]
    assert items[0]["date"] == "2026-08-01T10:00:00+00:00"
    assert "message_date" not in items[0]
    assert cursor is None and not has_more
    timeline = await database.list_timeline(account_id="alpha", visibility="private")
    assert timeline[0]["months"][0]["days"][0]["day"] == "2026-08-01"


@pytest.mark.asyncio
async def test_visibility_filter_and_local_title_are_preserved(database: Database) -> None:
    await database.upsert_media_index(media("alpha", 1, "2026-08-01T10:00:00+00:00"))
    await database.set_local_title(1, "local", "alpha")
    assert await database.set_media_visibility("alpha", 1, "public")
    public, _, _ = await database.list_media_index(
        account_id="alpha", limit=10, cursor=None, order="newest", kind="all", query="", visibility="public"
    )
    private, _, _ = await database.list_media_index(
        account_id="alpha", limit=10, cursor=None, order="newest", kind="all", query="", visibility="private"
    )
    assert public[0]["title"] == "local"
    assert private == []


@pytest.mark.asyncio
async def test_helper_provenance_promotes_existing_row_once_and_preserves_admin_override(database: Database) -> None:
    item = media("alpha", 9, "2026-08-19T10:00:00+00:00", "helper")
    await database.upsert_media_index(item)
    assert (await database.get_media_index("alpha", 9))["visibility"] == "private"

    await database.upsert_media_index(
        item,
        visibility="public",
        source_ingest_job_id=77,
        submitter_telegram_user_id="100",
    )
    assert (await database.get_media_index("alpha", 9))["visibility"] == "public"

    await database.set_media_visibility("alpha", 9, "private")
    await database.upsert_media_index(
        item,
        visibility="public",
        source_ingest_job_id=77,
        submitter_telegram_user_id="100",
    )
    assert (await database.get_media_index("alpha", 9))["visibility"] == "private"

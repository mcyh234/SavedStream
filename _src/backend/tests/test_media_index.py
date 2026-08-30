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
async def test_media_sorting_supports_all_columns_and_stable_pagination(database: Database) -> None:
    entries = [
        {**media("alpha", 10, "2026-08-03T10:00:00+00:00", "Zulu"), "kind": "video", "mime_type": "video/mp4", "size": 900},
        {**media("alpha", 20, "2026-08-01T10:00:00+00:00", "alpha"), "kind": "file", "mime_type": "application/pdf", "size": 100},
        {**media("alpha", 30, "2026-08-02T10:00:00+00:00", "Bravo"), "kind": "audio", "mime_type": "audio/mpeg", "size": 500},
    ]
    for entry in entries:
        await database.upsert_media_index(entry)

    title_page, title_cursor, title_more = await database.list_media_index(
        account_id="alpha", limit=2, cursor=None, order="newest", kind="all", query="",
        visibility="private", sort_by="title", sort_direction="asc",
    )
    assert [item["title"] for item in title_page] == ["alpha", "Bravo"]
    assert all("sort_value" not in item for item in title_page)
    assert title_more and isinstance(title_cursor, str)
    title_tail, _, title_tail_more = await database.list_media_index(
        account_id="alpha", limit=2, cursor=title_cursor, order="newest", kind="all", query="",
        visibility="private", sort_by="title", sort_direction="asc",
    )
    assert [item["title"] for item in title_tail] == ["Zulu"]
    assert not title_tail_more

    for field, direction, expected in [
        ("kind", "asc", [30, 20, 10]),
        ("size", "desc", [10, 30, 20]),
        ("date", "asc", [20, 30, 10]),
    ]:
        sorted_items, _, _ = await database.list_media_index(
            account_id="alpha", limit=10, cursor=None, order="newest", kind="all", query="",
            visibility="private", sort_by=field, sort_direction=direction,
        )
        assert [item["id"] for item in sorted_items] == expected


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


@pytest.mark.asyncio
async def test_document_backed_camera_media_uses_filename_capture_time_in_timeline(database: Database) -> None:
    await database.upsert_media_index({
        "account_id": "alpha",
        "id": 81,
        "kind": "file",
        "mime_type": "application/octet-stream",
        "size": 12 * 1024 * 1024,
        "filename": "IMG_20250923_003303_054.jpg",
        "original_title": "IMG_20250923_003303_054.jpg",
        "caption": "",
        "date": "2026-08-30T12:00:00+00:00",
        "has_thumbnail": True,
    })
    await database.upsert_media_index({
        "account_id": "alpha",
        "id": 82,
        "kind": "file",
        "mime_type": "application/octet-stream",
        "size": 20 * 1024 * 1024,
        "filename": "VID_20251004_231122_987.MOV",
        "original_title": "VID_20251004_231122_987.MOV",
        "caption": "",
        "date": "2026-08-30T12:00:00+00:00",
        "has_thumbnail": True,
    })
    await database.rebuild_timeline("alpha")

    image = await database.get_media_index("alpha", 81)
    video = await database.get_media_index("alpha", 82)
    assert image and image["kind"] == "image"
    assert image["mime_type"] == "image/jpeg"
    assert image["date"] == "2025-09-23T00:33:03.054000+00:00"
    assert video and video["kind"] == "video"
    assert video["mime_type"] == "video/quicktime"
    assert video["date"] == "2025-10-04T23:11:22.987000+00:00"

    timeline = await database.list_timeline(account_id="alpha", visibility="private")
    days = [
        day["day"]
        for year in timeline
        for month in year["months"]
        for day in month["days"]
    ]
    assert days == ["2025-10-04", "2025-09-23"]


@pytest.mark.asyncio
async def test_initialize_backfills_legacy_camera_documents_without_full_rescan(database: Database) -> None:
    import aiosqlite

    async with aiosqlite.connect(database.path) as db:
        await db.execute(
            """
            INSERT INTO media_index(
                account_id,message_id,kind,mime_type,size,filename,original_title,
                caption,message_date,date_year,date_month,date_day,duration,width,
                height,has_thumbnail,visibility,hidden,deleted,indexed_at,last_seen_at,
                requested_visibility,review_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "legacy",
                901,
                "file",
                "application/octet-stream",
                15 * 1024 * 1024,
                "IMG_20241231_235959_321.jpg",
                "IMG_20241231_235959_321.jpg",
                "",
                "2026-08-30T12:00:00+00:00",
                2026,
                "2026-08",
                "2026-08-30",
                None,
                None,
                None,
                1,
                "private",
                0,
                0,
                "2026-08-30T12:00:00+00:00",
                "2026-08-30T12:00:00+00:00",
                "private",
                "not_required",
            ),
        )
        await db.execute(
            "DELETE FROM settings WHERE key='media_filename_metadata_version'"
        )
        await db.commit()

    await database.initialize()

    item = await database.get_media_index("legacy", 901)
    assert item and item["kind"] == "image"
    assert item["mime_type"] == "image/jpeg"
    assert item["date"] == "2024-12-31T23:59:59.321000+00:00"
    timeline = await database.list_timeline(account_id="legacy", visibility="private")
    assert timeline[0]["months"][0]["days"][0]["day"] == "2024-12-31"

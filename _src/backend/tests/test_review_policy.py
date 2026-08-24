from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from app.database import Database


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "review.db")
    await instance.initialize()
    return instance


def media(account: str, message_id: int, *, owner: str | None = None, batch: str | None = None) -> dict:
    return {
        "account_id": account,
        "id": message_id,
        "kind": "video",
        "mime_type": "video/mp4",
        "size": 1024,
        "filename": f"{message_id}.mp4",
        "original_title": f"Media {message_id}",
        "caption": "",
        "date": "2026-08-19T00:00:00+00:00",
        "has_thumbnail": True,
        "submitter_telegram_user_id": owner,
        "source_ingest_job_id": message_id if owner else None,
        "review_batch_id": batch,
    }


@pytest.mark.asyncio
async def test_public_request_is_pending_then_global_after_approval(database: Database) -> None:
    await database.upsert_media_index(
        media("alpha", 1, owner="100"),
        visibility="private",
        source_ingest_job_id=1,
        submitter_telegram_user_id="100",
        requested_visibility="public",
        review_status="pending",
    )
    public, _, _ = await database.list_media_index(
        account_id=None,
        limit=20,
        cursor=None,
        order="newest",
        kind="all",
        query="",
        visibility="public",
    )
    assert public == []
    own, _, _ = await database.list_media_index(
        account_id=None,
        limit=20,
        cursor=None,
        order="newest",
        kind="all",
        query="",
        visibility="all",
        owner_telegram_user_id="100",
    )
    assert [item["id"] for item in own] == [1]

    reviewed = await database.review_media("alpha", 1, "approved", reviewed_by="admin")
    assert reviewed and reviewed["visibility"] == "public"
    assert reviewed["review_status"] == "approved"
    public, _, _ = await database.list_media_index(
        account_id=None,
        limit=20,
        cursor=None,
        order="newest",
        kind="all",
        query="",
        visibility="public",
    )
    assert [item["id"] for item in public] == [1]
    assert await database.list_media_reviews(status="pending") == []


@pytest.mark.asyncio
async def test_rejected_owner_access_and_album_review_are_isolated(database: Database) -> None:
    for message_id in (10, 11):
        await database.upsert_media_index(
            media("alpha", message_id, owner="100", batch="album-1"),
            visibility="private",
            source_ingest_job_id=message_id,
            submitter_telegram_user_id="100",
            requested_visibility="public",
            review_status="pending",
            review_batch_id="album-1",
        )
    result = await database.review_media("alpha", 10, "rejected", reason="不符合规则")
    assert result and result["review_status"] == "rejected"
    first = await database.get_media_index("alpha", 10, include_provenance=True)
    second = await database.get_media_index("alpha", 11, include_provenance=True)
    assert first and second
    assert first["review_status"] == second["review_status"] == "rejected"
    assert first["visibility"] == second["visibility"] == "private"
    assert len(await database.list_media_reviews(status="rejected")) == 2
    outbox = await database.list_review_sync_outbox()
    assert {item["job_id"] for item in outbox} == {10, 11}


@pytest.mark.asyncio
async def test_legacy_helper_public_row_is_demoted_on_reinitialize(tmp_path: Path) -> None:
    database = Database(tmp_path / "legacy-helper.db")
    await database.initialize()
    await database.upsert_media_index(
        media("alpha", 99, owner="100"),
        visibility="public",
        source_ingest_job_id=99,
        submitter_telegram_user_id="100",
        requested_visibility="public",
        review_status="approved",
    )
    await database.initialize()
    item = await database.get_media_index("alpha", 99, include_provenance=True)
    assert item and item["visibility"] == "private" and item["review_status"] == "pending"


@pytest.mark.asyncio
async def test_tombstone_media_scrubs_content_and_keeps_minimal_deletion_audit(database: Database) -> None:
    await database.upsert_media_index(
        media("alpha", 120, owner="100", batch="bad-album"),
        visibility="private",
        source_ingest_job_id=120,
        submitter_telegram_user_id="100",
        requested_visibility="public",
        review_status="pending",
        review_batch_id="bad-album",
    )
    await database.upsert_media_index(
        media("alpha", 121, owner="100", batch="bad-album"),
        visibility="private",
        source_ingest_job_id=121,
        submitter_telegram_user_id="100",
        requested_visibility="public",
        review_status="pending",
        review_batch_id="bad-album",
    )

    deleted = await database.tombstone_media(
        "alpha",
        120,
        reason="恶意软件或危险文件",
        deleted_by="admin",
    )

    assert {item["id"] for item in deleted} == {120, 121}
    assert all(item["deleted"] and item["size"] == 0 and item["filename"] == "[deleted]" for item in deleted)
    assert await database.list_media_reviews(status="pending") == []
    async with aiosqlite.connect(database.path) as connection:
        rows = await (await connection.execute(
            "SELECT account_id,message_id,submitter_telegram_user_id,reason FROM media_deletion_events ORDER BY message_id"
        )).fetchall()
    assert rows == [
        ("alpha", 120, "100", "恶意软件或危险文件"),
        ("alpha", 121, "100", "恶意软件或危险文件"),
    ]


@pytest.mark.asyncio
async def test_deleted_media_is_not_resurrected_by_a_later_index_pass(database: Database) -> None:
    await database.upsert_media_index(
        media("alpha", 130, owner="100"),
        visibility="private",
        source_ingest_job_id=130,
        submitter_telegram_user_id="100",
        requested_visibility="public",
        review_status="pending",
    )
    await database.tombstone_media("alpha", 130, reason="恶意软件")

    # A normal Telegram sync does not carry Helper Bot provenance.  It must
    # update metadata if necessary, but must not make the deleted row live
    # again or put it back into the review queue.
    await database.upsert_media_index(
        {
            **media("alpha", 130, owner=None),
            "filename": "should-not-return.mp4",
            "original_title": "Should not return",
        },
        visibility="private",
    )

    deleted = await database.get_media_index(
        "alpha", 130, include_deleted=True, include_provenance=True
    )
    assert deleted and deleted["deleted"] is True
    assert deleted["filename"] == "[deleted]"
    assert await database.list_media_reviews(status="pending") == []

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from app.database import Database
from app.media_indexer import MediaIndexer


def media(message_id: int, date: str) -> dict:
    return {
        "id": message_id,
        "kind": "video",
        "mime_type": "video/mp4",
        "size": 100 + message_id,
        "filename": f"movie-{message_id}.mp4",
        "original_title": f"Movie {message_id}",
        "caption": f"Movie {message_id}",
        "date": date,
        "has_thumbnail": True,
    }


class FakeTeleBox:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.list_calls = 0

    async def sync_saved_media(self, **kwargs):
        self.calls.append(dict(kwargs))
        if kwargs["mode"] == "full":
            if kwargs.get("cursor") is None:
                return {
                    "items": [media(5, "2026-08-01T00:00:00+00:00"), media(7, "2026-08-02T00:00:00+00:00")],
                    "next_cursor": 5,
                    "has_more": True,
                }
            assert kwargs["cursor"] == 5
            return {
                "items": [media(1, "2026-07-01T00:00:00+00:00"), media(3, "2026-07-02T00:00:00+00:00")],
                "next_cursor": None,
                "has_more": False,
            }
        assert kwargs["after_id"] == 7
        return {
            "items": [media(8, "2026-08-03T00:00:00+00:00")],
            "next_cursor": None,
            "has_more": False,
        }

    async def accounts(self):
        self.list_calls += 1
        return {"items": []}

    async def jobs(self, **kwargs):
        return {"items": [], "has_more": False}


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "indexer.db")
    await instance.initialize()
    return instance


@pytest.mark.asyncio
async def test_full_then_incremental_sync_persists_cursors_and_uses_local_index(database: Database) -> None:
    telegram = FakeTeleBox()
    indexer = MediaIndexer(database, telegram)  # type: ignore[arg-type]
    await database.upsert_media_index(media(999, "2025-01-01T00:00:00+00:00") | {"account_id": "alpha"})

    full = await indexer.sync_account("alpha")
    assert full["status"] == "ready"
    assert full["high_watermark_id"] == 7
    assert [call["cursor"] for call in telegram.calls[:2]] == [None, 5]
    assert telegram.list_calls == 0

    incremental = await indexer.sync_account("alpha")
    assert incremental["status"] == "ready"
    assert incremental["high_watermark_id"] == 8
    assert telegram.calls[-1]["mode"] == "incremental"
    assert telegram.calls[-1]["after_id"] == 7

    items, _, has_more = await database.list_media_index(
        account_id="alpha",
        limit=20,
        cursor=None,
        order="newest",
        kind="all",
        query="",
        visibility="private",
    )
    assert [item["id"] for item in items] == [8, 7, 5, 3, 1]
    assert not has_more
    deleted = await database.get_media_index("alpha", 999, include_deleted=True)
    assert deleted and deleted["deleted"] is True


@pytest.mark.asyncio
async def test_account_syncs_are_serialized(database: Database) -> None:
    telegram = FakeTeleBox()
    indexer = MediaIndexer(database, telegram)  # type: ignore[arg-type]
    await asyncio.gather(
        indexer.sync_account("alpha"),
        indexer.sync_account("alpha"),
    )
    # The second caller sees the completed checkpoint and performs the
    # incremental pass instead of racing another full backfill.
    assert telegram.calls[0]["mode"] == "full"
    assert any(call["mode"] == "incremental" for call in telegram.calls[1:])


class FakeIngestTeleBox(FakeTeleBox):
    def __init__(self, jobs: list[dict]) -> None:
        super().__init__()
        self.completed_jobs = jobs
        self.job_calls: list[dict] = []

    async def jobs(self, **kwargs):
        self.job_calls.append(dict(kwargs))
        updated_after = int(kwargs.get("updated_after") or 0)
        after_job_id = int(kwargs.get("after_job_id") or 0)
        items = [
            job
            for job in self.completed_jobs
            if int(job["updated_at"]) > updated_after
            or (int(job["updated_at"]) == updated_after and int(job["id"]) > after_job_id)
        ]
        return {"items": items, "has_more": False}

    async def get_media_message(self, account_id: str, message_id: int):
        if message_id == 9999:
            raise RuntimeError("stale Telegram message")
        return {"account_id": account_id, "id": message_id}, media(
            message_id, "2026-08-19T08:00:00+00:00"
        )


class TransientIngestTeleBox(FakeIngestTeleBox):
    async def get_media_message(self, account_id: str, message_id: int):
        if message_id == 8888:
            raise RuntimeError("TeleBox request failed: temporary metadata outage")
        return await super().get_media_message(account_id, message_id)


@pytest.mark.asyncio
async def test_completed_helper_upload_is_public_only_for_approved_submitter(database: Database) -> None:
    await database.upsert_media_user({
        "telegram_user_id": "100",
        "account_id": "alpha",
        "username": "alice",
        "display_name": "Alice",
    })
    await database.set_media_user_status("100", "approved")
    telegram = FakeIngestTeleBox([
        {
            "id": 1,
            "account_id": "alpha",
            "source_chat_id": "100",
            "submitter_telegram_user_id": "100",
            "saved_message_id": 51,
            "status": "completed",
            "updated_at": 1_000,
        },
        {
            "id": 2,
            "account_id": "alpha",
            "source_chat_id": "200",
            "submitter_telegram_user_id": "200",
            "saved_message_id": 52,
            "status": "completed",
            "updated_at": 1_001,
        },
    ])
    indexer = MediaIndexer(database, telegram)  # type: ignore[arg-type]

    result = await indexer.reconcile_completed_ingest_jobs()

    assert result["processed"] == 2
    assert (await database.get_media_index("alpha", 51))["visibility"] == "public"
    assert (await database.get_media_index("alpha", 52))["visibility"] == "private"
    state = await database.get_ingest_reconcile_state()
    assert state["last_updated_at"] == 1_001
    assert state["last_job_id"] == 2

    replay = await indexer.reconcile_completed_ingest_jobs()
    assert replay["processed"] == 0
    assert telegram.job_calls[-1]["updated_after"] == 1_001
    public, _, _ = await database.list_media_index(
        account_id="alpha",
        limit=10,
        cursor=None,
        order="newest",
        kind="all",
        query="",
        visibility="public",
    )
    assert [item["id"] for item in public] == [51]


@pytest.mark.asyncio
async def test_helper_upload_stays_private_when_approval_targets_another_account(database: Database) -> None:
    await database.upsert_media_user({
        "telegram_user_id": "100",
        "account_id": "beta",
        "username": "alice",
        "display_name": "Alice",
    })
    await database.set_media_user_status("100", "approved")
    telegram = FakeIngestTeleBox([{
        "id": 7,
        "account_id": "alpha",
        "source_chat_id": "100",
        "submitter_telegram_user_id": "100",
        "saved_message_id": 70,
        "status": "completed",
        "updated_at": 7_000,
    }])
    indexer = MediaIndexer(database, telegram)  # type: ignore[arg-type]

    await indexer.reconcile_completed_ingest_jobs()

    assert (await database.get_media_index("alpha", 70))["visibility"] == "private"


@pytest.mark.asyncio
async def test_stale_completed_job_does_not_block_new_pending_review(database: Database) -> None:
    telegram = FakeIngestTeleBox([
        {
            "id": 90,
            "account_id": "alpha",
            "source_chat_id": "100",
            "submitter_telegram_user_id": "100",
            "saved_message_id": 9999,
            "status": "completed",
            "updated_at": 9_000,
            "requested_visibility": "public",
            "review_status": "pending",
        },
        {
            "id": 91,
            "account_id": "alpha",
            "source_chat_id": "101",
            "submitter_telegram_user_id": "101",
            "saved_message_id": 91,
            "status": "completed",
            "updated_at": 9_001,
            "requested_visibility": "public",
            "review_status": "pending",
        },
    ])
    indexer = MediaIndexer(database, telegram)  # type: ignore[arg-type]

    result = await indexer.reconcile_completed_ingest_jobs()

    assert result["processed"] == 1
    pending = await database.list_media_reviews(status="pending")
    assert [item["id"] for item in pending] == [91]
    state = await database.get_ingest_reconcile_state()
    assert state["last_job_id"] == 91
    assert "job #90" in str(state["error"])


@pytest.mark.asyncio
async def test_transient_completed_job_remains_visible_as_review_placeholder(database: Database) -> None:
    telegram = TransientIngestTeleBox([{
        "id": 88,
        "account_id": "alpha",
        "source_chat_id": "100",
        "submitter_telegram_user_id": "100",
        "saved_message_id": 8888,
        "status": "completed",
        "updated_at": 8_800,
        "created_at": 8_700,
        "requested_visibility": "public",
        "review_status": "pending",
        "source_file_size": 2048,
        "source_filename": "pending-video.mp4",
        "source_mime_type": "video/mp4",
    }])
    indexer = MediaIndexer(database, telegram)  # type: ignore[arg-type]

    result = await indexer.reconcile_completed_ingest_jobs()

    assert result["processed"] == 0
    pending = await database.list_media_reviews(status="pending")
    assert [item["id"] for item in pending] == [8888]
    assert pending[0]["filename"] == "pending-video.mp4"
    assert pending[0]["size"] == 2048
    assert pending[0]["has_thumbnail"] is False

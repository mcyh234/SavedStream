from __future__ import annotations

from pathlib import Path

import pytest

from app.database import Database
from app.replication import DisasterRecoveryManager


class FakeTeleBox:
    def __init__(self) -> None:
        self.copies: list[dict] = []

    async def accounts(self):
        return {"items": [
            {"id": "primary", "label": "Primary", "state": "authenticated"},
            {"id": "replica", "label": "Replica", "state": "authenticated"},
        ]}

    async def account_health(self, account_id: str):
        return {"account_id": account_id, "healthy": True}

    async def replication_copy(self, **payload):
        self.copies.append(payload)
        return {"target_message_id": 9001, "size": 123, "content_sha256": "a" * 64}


@pytest.mark.asyncio
async def test_account_groups_and_live_replication_are_durable(tmp_path: Path) -> None:
    database = Database(tmp_path / "savedstream.db")
    await database.initialize()
    await database.ensure_account_group("library", name="Library", primary_account_id="primary")
    await database.add_account_group_member("library", "replica", role="replica", priority=10)
    await database.update_account_group_member("library", "replica", sync_status="ready")
    await database.upsert_media_index({
        "account_id": "primary", "id": 7, "kind": "file", "mime_type": "application/octet-stream",
        "size": 123, "filename": "sample.bin", "original_title": "sample.bin", "caption": "",
        "date": "2026-08-25T00:00:00+00:00", "has_thumbnail": False,
    }, account_group_id="library", logical_media_id="primary:7", origin_account_id="primary", origin_message_id=7)

    telegram = FakeTeleBox()
    manager = DisasterRecoveryManager(database, telegram)  # type: ignore[arg-type]
    assert await manager.enqueue_media("primary", 7) == 1
    queued = await database.list_replication_jobs(status="queued")
    assert len(queued) == 1

    await manager._process_job(queued[0])
    completed = await database.get_replication_job(str(queued[0]["id"]))
    assert completed and completed["status"] == "completed"
    mapping = await database.get_replication_mapping("library", "replica", "primary:7")
    assert mapping and mapping["target_message_id"] == 9001
    replica = await database.get_media_index("replica", 9001, include_provenance=True)
    assert replica and replica["owner_user_id"] is None and replica["logical_media_id"] == "primary:7"


@pytest.mark.asyncio
async def test_failover_updates_active_account_without_automatic_failback(tmp_path: Path) -> None:
    database = Database(tmp_path / "savedstream.db")
    await database.initialize()
    await database.ensure_account_group("library", name="Library", primary_account_id="primary")
    await database.add_account_group_member("library", "replica", role="replica", priority=10)
    await database.update_account_group_member("library", "replica", sync_status="ready")
    await database.record_failover("library", "primary", "replica", "health threshold reached", 3)
    group = await database.get_account_group("library")
    assert group and group["active_account_id"] == "replica"
    assert group["status"] == "failed_over"


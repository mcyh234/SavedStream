from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.cache import DiskCache
from app.database import Database
from app.main import ADMIN_COOKIE, app, get_cache, get_database, get_telegram, signer


class FakeTeleBox:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, int]] = []

    async def status(self) -> dict:
        return {"authenticated": True, "state": "authenticated", "error": None}

    async def accounts(self) -> dict:
        return {"items": [{"id": "alpha", "label": "Alpha", "state": "authenticated"}]}

    async def resolve_account(self, account_id: str | None) -> str:
        return account_id or "alpha"

    async def helper_bot_status(self) -> dict:
        return {"configured": True, "username": "savedstream_bot", "token": "masked"}

    async def jobs(self, **kwargs):
        return {"items": [], "has_more": False}

    async def bindings(self, **kwargs):
        return {"items": []}

    async def helper_bot_rate_limit(self) -> dict:
        return {
            "per_user_files_24h": 20,
            "per_user_bytes_24h": 10_000_000_000,
            "per_user_concurrent": 2,
            "max_file_bytes": 2_000_000_000,
            "global_files_per_minute": 30,
            "max_album_items": 10,
            "max_album_bytes": 2_000_000_000,
        }

    async def set_helper_bot_rate_limit(self, payload: dict) -> dict:
        return payload

    async def delete_media(self, account_id: str, message_id: int, **kwargs):
        self.deleted.append((account_id, int(message_id)))
        return {"ok": True}

    async def get_media_message(self, account_id: str, message_id: int):
        return {"account_id": account_id, "id": message_id}, {
            "id": message_id,
            "kind": "image",
            "mime_type": "image/jpeg",
            "size": 4,
            "filename": "poster.jpg",
            "original_title": "Poster",
            "caption": "",
            "date": "2026-08-01T00:00:00+00:00",
            "has_thumbnail": True,
        }

    async def download_thumbnail(self, message: dict) -> bytes:
        return b"\xff\xd8\xff\xd9"

    async def download_chunk(self, message: dict, offset: int, file_size: int) -> bytes:
        return b"data"

    @staticmethod
    def media_cache_key(message: dict, item: dict) -> str:
        return f"{message['account_id']}:{message['id']}:{item['size']}"


def media(message_id: int, *, visibility: str = "private", hidden: bool = False) -> dict:
    return {
        "account_id": "alpha",
        "id": message_id,
        "kind": "image",
        "mime_type": "image/jpeg",
        "size": 100,
        "filename": f"{message_id}.jpg",
        "original_title": f"Media {message_id}",
        "caption": "",
        "date": "2026-08-01T00:00:00+00:00",
        "has_thumbnail": True,
        "hidden": hidden,
        "visibility": visibility,
    }


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "library.db")
    await instance.initialize()
    return instance


# ---------------------------------------------------------------------------
# Hidden visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hidden_media_is_admin_only(database: Database) -> None:
    await database.upsert_media_index(media(1, visibility="public"))
    hidden = await database.set_media_hidden("alpha", 1, True)
    assert hidden and hidden["hidden"] is True and hidden["visibility"] == "hidden"

    public, _, _ = await database.list_media_index(
        account_id=None, limit=10, cursor=None, order="newest", kind="all", query="", visibility="public"
    )
    assert public == []
    private, _, _ = await database.list_media_index(
        account_id=None, limit=10, cursor=None, order="newest", kind="all", query="", visibility="private"
    )
    assert private == []
    admin_all, _, _ = await database.list_media_index(
        account_id=None, limit=10, cursor=None, order="newest", kind="all", query="", visibility="all"
    )
    assert [item["id"] for item in admin_all] == [1]
    assert admin_all[0]["visibility"] == "hidden"
    only_hidden, _, _ = await database.list_media_index(
        account_id=None, limit=10, cursor=None, order="newest", kind="all", query="", visibility="hidden"
    )
    assert [item["id"] for item in only_hidden] == [1]

    restored = await database.set_media_hidden("alpha", 1, False)
    assert restored and restored["hidden"] is False and restored["visibility"] == "private"


@pytest.mark.asyncio
async def test_owner_cannot_see_hidden_media(database: Database) -> None:
    await database.upsert_media_index(
        media(7),
        submitter_telegram_user_id="100",
        source_ingest_job_id=7,
        requested_visibility="private",
        review_status="not_required",
    )
    await database.set_media_hidden("alpha", 7, True)
    own, _, _ = await database.list_media_index(
        account_id=None, limit=10, cursor=None, order="newest", kind="all", query="",
        visibility="all", owner_telegram_user_id="100",
    )
    assert own == []


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_folder_hierarchy_create_rename_move_delete(database: Database) -> None:
    root = await database.create_folder("相册", parent_id=0, created_by="admin")
    child = await database.create_folder("旅行", parent_id=int(root["id"]), created_by="admin")
    grandchild = await database.create_folder("2026", parent_id=int(child["id"]), created_by="admin")

    assert await database.list_folders() is not None
    with pytest.raises(ValueError):
        await database.create_folder("旅行", parent_id=int(root["id"]))
    with pytest.raises(ValueError):
        await database.move_folder(int(root["id"]), int(grandchild["id"]))

    renamed = await database.rename_folder(int(grandchild["id"]), "2027")
    assert renamed and renamed["name"] == "2027"

    moved = await database.move_folder(int(child["id"]), 0)
    assert moved and moved["parent_id"] == 0

    deleted = await database.delete_folder(int(root["id"]))
    assert deleted == 3
    assert await database.list_folders() == []


@pytest.mark.asyncio
async def test_folder_items_and_media_filter(database: Database) -> None:
    await database.upsert_media_index(media(1))
    await database.upsert_media_index(media(2))
    folder = await database.create_folder("收藏", parent_id=0)
    added = await database.set_folder_items(int(folder["id"]), [{"account_id": "alpha", "message_id": 1}])
    assert added == 1

    items, _, _ = await database.list_media_index(
        account_id=None, limit=10, cursor=None, order="newest", kind="all", query="",
        visibility="all", folder_id=int(folder["id"]),
    )
    assert [item["id"] for item in items] == [1]

    removed = await database.remove_folder_items(int(folder["id"]), [{"account_id": "alpha", "message_id": 1}])
    assert removed == 1
    items, _, _ = await database.list_media_index(
        account_id=None, limit=10, cursor=None, order="newest", kind="all", query="",
        visibility="all", folder_id=int(folder["id"]),
    )
    assert items == []


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notifications_delivery_read_and_broadcast(database: Database) -> None:
    import aiosqlite

    async with aiosqlite.connect(database.path) as connection:
        await connection.execute(
            "INSERT INTO auth_users(username_normalized,username_display,password_hash,role,status,display_name,binding_sync_status,created_at) "
            "VALUES('alice','alice','x','user','approved','Alice','not_required','2026-08-01T00:00:00+00:00')"
        )
        await connection.execute(
            "INSERT INTO auth_users(username_normalized,username_display,password_hash,role,status,display_name,binding_sync_status,created_at) "
            "VALUES('bob','bob','x','user','approved','Bob','not_required','2026-08-01T00:00:00+00:00')"
        )
        await connection.commit()

    sent = await database.create_notification_broadcast("system", "维护通知", "今晚维护")
    assert sent == 2
    assert await database.unread_notification_count(1) == 1
    assert await database.unread_notification_count(2) == 1

    items, next_cursor, has_more = await database.list_notifications(1, limit=10)
    assert len(items) == 1 and not has_more and next_cursor is None
    assert items[0]["title"] == "维护通知"

    marked = await database.mark_notifications_read(1, [int(items[0]["id"])])
    assert marked == 1
    assert await database.unread_notification_count(1) == 0

    removed = await database.delete_notifications(2)
    assert removed == 1
    items, _, _ = await database.list_notifications(2, limit=10)
    assert items == []


# ---------------------------------------------------------------------------
# API: hidden visibility patch + folder routes
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api_client(tmp_path: Path):
    database = Database(tmp_path / "api.db")
    await database.initialize()
    telegram = FakeTeleBox()

    async def cache_limit() -> int:
        return 1024 * 1024 * 1024

    cache = DiskCache(tmp_path / "cache", cache_limit, "00" * 32)
    await cache.initialize()
    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_telegram] = lambda: telegram
    app.dependency_overrides[get_cache] = lambda: cache
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client, database, telegram
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_admin_can_hide_media_via_api(api_client) -> None:
    client, database, _telegram = api_client
    client.cookies.set(ADMIN_COOKIE, signer.issue("admin", "control", 60))
    await database.upsert_media_index(media(1, visibility="public"))

    hidden = await client.patch(
        "/api/admin/media/1/visibility?account=alpha", json={"visibility": "hidden"}
    )
    assert hidden.status_code == 200
    assert hidden.json()["visibility"] == "hidden"

    listing = await client.get("/api/media?scope=all")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["visibility"] == "hidden"

    restored = await client.patch(
        "/api/admin/media/1/visibility?account=alpha", json={"visibility": "private"}
    )
    assert restored.status_code == 200
    assert restored.json()["visibility"] == "private"


@pytest.mark.asyncio
async def test_admin_folder_routes(api_client) -> None:
    client, database, _telegram = api_client
    client.cookies.set(ADMIN_COOKIE, signer.issue("admin", "control", 60))
    await database.upsert_media_index(media(1))

    created = await client.post("/api/admin/folders", json={"name": "相册", "parent_id": 0})
    assert created.status_code == 200
    folder_id = created.json()["id"]

    duplicate = await client.post("/api/admin/folders", json={"name": "相册", "parent_id": 0})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "FOLDER_CONFLICT"

    added = await client.put(
        f"/api/admin/folders/{folder_id}/items",
        json={"items": [{"account_id": "alpha", "message_id": 1}]},
    )
    assert added.status_code == 200 and added.json()["added"] == 1

    folders = await client.get("/api/folders")
    assert folders.status_code == 200
    assert folders.json()["items"][0]["item_count"] == 1

    media_page = await client.get(f"/api/media?folder_id={folder_id}&scope=all")
    assert media_page.status_code == 200
    assert [item["id"] for item in media_page.json()["items"]] == [1]

    removed = await client.delete(
        f"/api/admin/folders/{folder_id}/items",
        json={"items": [{"account_id": "alpha", "message_id": 1}]},
    )
    assert removed.status_code == 200 and removed.json()["removed"] == 1

    deleted = await client.delete(f"/api/admin/folders/{folder_id}")
    assert deleted.status_code == 200 and deleted.json()["deleted"] == 1

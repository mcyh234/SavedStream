from __future__ import annotations

import base64
from pathlib import Path

import httpx
import aiosqlite
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.database import Database
from app.cache import DiskCache
from app.main import ADMIN_COOKIE, DEVICE_COOKIE, VIEWER_COOKIE, app, get_cache, get_database, get_telegram, signer, _hash_public_key
from app.telebox_client import InvalidWebLoginCode


class FakeTeleBox:
    def __init__(self) -> None:
        self.identities = {
            "valid-code-alpha-123456": {
                "telegram_user_id": "100",
                "account_id": "alpha",
                "username": "alice",
                "display_name": "Alice",
            }
        }
        self.completed_jobs: list[dict] = []
        self.deleted_jobs: list[int] = []
        self.binding_updates: list[dict] = []

    async def consume_web_login_code(self, code: str) -> dict:
        identity = self.identities.pop(code, None)
        if not identity:
            raise InvalidWebLoginCode("invalid")
        return identity

    async def helper_bot_status(self) -> dict:
        return {"configured": True, "username": "savedstream_bot", "token": "masked"}

    async def status(self) -> dict:
        return {"authenticated": True, "state": "authenticated", "error": None}

    async def accounts(self) -> dict:
        return {
            "items": [
                {"id": "alpha", "label": "Alpha", "state": "authenticated"},
                {"id": "beta", "label": "Beta", "state": "authenticated"},
            ]
        }

    async def resolve_account(self, account_id: str) -> str:
        return account_id

    async def list_saved_media(self, **kwargs):
        return [], None, False

    async def jobs(self, **kwargs):
        updated_after = int(kwargs.get("updated_after") or 0)
        after_job_id = int(kwargs.get("after_job_id") or 0)
        items = [
            item for item in self.completed_jobs
            if int(item.get("updated_at") or 0) > updated_after
            or (int(item.get("updated_at") or 0) == updated_after and int(item.get("id") or 0) > after_job_id)
        ]
        return {"items": items, "has_more": False}

    async def delete_ingest_job(self, job_id: int, **kwargs):
        self.deleted_jobs.append(int(job_id))
        return {"id": int(job_id), "status": "deleted"}

    async def delete_media(self, account_id: str, message_id: int, **kwargs):
        return {"ok": True, "account_id": account_id, "message_id": message_id}

    async def set_binding_status(self, telegram_user_id: str, **kwargs):
        self.binding_updates.append({"telegram_user_id": telegram_user_id, **kwargs})
        return {"telegram_user_id": telegram_user_id, "enabled": int(bool(kwargs.get("enabled")))}

    async def get_media_message(self, account_id: str, message_id: int):
        return {"account_id": account_id, "id": message_id}, {
            "id": message_id,
            "kind": "image",
            "mime_type": "image/jpeg",
            "size": 4,
            "filename": "poster.jpg",
            "original_title": "Poster",
            "caption": "Poster",
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


async def enable_public_access(database: Database, client: httpx.AsyncClient, key: str = "public-key") -> None:
    await database.set_setting("public_album_key_hash", _hash_public_key(key))
    await database.set_setting("public_album_key_version", "2")
    await database.set_setting("public_album_enabled", "1")
    response = await client.post("/api/public/login", json={"key": key})
    assert response.status_code == 200


@pytest_asyncio.fixture
async def access_client(tmp_path: Path):
    database = Database(tmp_path / "access.db")
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
        yield client, database
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_pending_user_cannot_access_and_approved_user_is_bound_to_one_account(access_client) -> None:
    client, database = access_client
    response = await client.post("/api/access/telegram", json={"code": "valid-code-alpha-123456"})
    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert (await client.get("/api/access/telegram/status")).json()["status"] == "pending"

    pending = await client.get("/api/media?account=alpha")
    assert pending.status_code == 403
    assert pending.json()["detail"]["code"] == "ACCESS_PENDING"

    await database.set_media_user_status("100", "approved")
    assert (await client.get("/api/access/telegram/status")).json()["status"] == "approved"
    await enable_public_access(database, client)
    accounts = await client.get("/api/accounts")
    assert [item["id"] for item in accounts.json()["items"]] == ["alpha", "beta"]
    allowed = await client.get("/api/media?account=alpha")
    assert allowed.status_code == 200
    cross_account = await client.get("/api/media?account=beta")
    # Public media is global; the bound account no longer limits the public
    # album's account selector.
    assert cross_account.status_code == 200


@pytest.mark.asyncio
async def test_login_code_cannot_be_reused_and_session_is_stored_hashed(access_client) -> None:
    client, database = access_client
    first = await client.post("/api/access/telegram", json={"code": "valid-code-alpha-123456"})
    assert first.status_code == 200
    raw_token = client.cookies.get("savedstream_access")
    assert raw_token
    async with aiosqlite.connect(database.path) as connection:
        row = await (await connection.execute("SELECT token_hash FROM access_sessions")).fetchone()
    assert row and row[0] != raw_token and raw_token not in row[0]
    second = await client.post("/api/access/telegram", json={"code": "valid-code-alpha-123456"})
    assert second.status_code == 401


@pytest.mark.asyncio
async def test_relogin_does_not_reset_an_existing_admin_decision(access_client) -> None:
    _, database = access_client
    identity = {
        "telegram_user_id": "100",
        "account_id": "alpha",
        "username": "alice",
        "display_name": "Alice",
    }
    await database.upsert_media_user(identity)
    await database.set_media_user_status("100", "approved")
    assert (await database.upsert_media_user(identity))["status"] == "approved"
    await database.set_media_user_status("100", "disabled")
    assert (await database.upsert_media_user(identity))["status"] == "disabled"


@pytest.mark.asyncio
async def test_rebinding_to_a_different_account_requires_new_approval(access_client) -> None:
    _, database = access_client
    identity = {
        "telegram_user_id": "100",
        "account_id": "alpha",
        "username": "alice",
        "display_name": "Alice",
    }
    await database.upsert_media_user(identity)
    await database.set_media_user_status("100", "approved")
    changed = {**identity, "account_id": "beta"}
    assert (await database.upsert_media_user(changed))["status"] == "pending"


@pytest.mark.asyncio
async def test_disabling_user_revokes_existing_session(access_client) -> None:
    client, database = access_client
    await client.post("/api/access/telegram", json={"code": "valid-code-alpha-123456"})
    await database.set_media_user_status("100", "approved")
    await enable_public_access(database, client)
    assert (await client.get("/api/media?account=alpha")).status_code == 200

    await database.set_media_user_status("100", "disabled")
    assert (await client.get("/api/media?account=alpha")).status_code == 403


@pytest.mark.asyncio
async def test_admin_can_see_all_accounts_and_viewer_password_is_not_authorized(access_client) -> None:
    client, database = access_client
    client.cookies.set(ADMIN_COOKIE, signer.issue("admin", "control", 60))
    accounts = await client.get("/api/accounts")
    assert accounts.status_code == 200
    assert {item["id"] for item in accounts.json()["items"]} == {"alpha", "beta"}

    await database.set_setting("viewer_key_hash", "legacy-hash")
    client.cookies.delete(ADMIN_COOKIE)
    client.cookies.set(VIEWER_COOKIE, signer.issue("viewer", "legacy-hash", 60))
    assert (await client.get("/api/media?account=alpha")).status_code == 401
    assert (await client.post("/api/access/login", json={"key": "legacy"})).status_code == 410


@pytest.mark.asyncio
async def test_public_key_requires_admin_toggle_and_telegram_approval(access_client) -> None:
    client, database = access_client
    await database.set_setting("public_album_key_hash", _hash_public_key("public-key"))
    await database.set_setting("public_album_key_version", "3")
    assert (await client.post("/api/public/login", json={"key": "public-key"})).status_code == 403

    await database.set_setting("public_album_enabled", "1")
    assert (await client.post("/api/public/login", json={"key": "public-key"})).status_code == 200
    # A valid public key alone is not enough; the Telegram identity must be
    # bound and approved as the second factor.
    assert (await client.get("/api/media?account=alpha")).status_code == 403

    await client.post("/api/access/telegram", json={"code": "valid-code-alpha-123456"})
    await database.set_media_user_status("100", "approved")
    assert (await client.get("/api/media?account=alpha")).status_code == 200
    status = (await client.get("/api/status")).json()
    assert status["media_authenticated"] is True
    assert len(status["media_session_id"]) == 32


@pytest.mark.asyncio
async def test_public_device_key_cookie_is_session_scoped_and_logout_clears_it(access_client) -> None:
    client, database = access_client
    await client.post("/api/access/telegram", json={"code": "valid-code-alpha-123456"})
    await database.set_media_user_status("100", "approved")
    await enable_public_access(database, client)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    spki = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    encoded = base64.urlsafe_b64encode(spki).rstrip(b"=").decode("ascii")
    response = await client.post(
        "/api/security/device-key",
        json={
            "device_public_key": encoded,
            "key_format": "spki-rsa-oaep-v1",
            "persistence": "session",
        },
    )
    assert response.status_code == 200
    device_cookie = next(value for value in response.headers.get_list("set-cookie") if value.startswith(f"{DEVICE_COOKIE}="))
    assert "Max-Age=" not in device_cookie
    assert client.cookies.get(DEVICE_COOKIE)

    logout = await client.post("/api/access/logout")
    assert logout.status_code == 200
    assert client.cookies.get(DEVICE_COOKIE) is None
    assert client.cookies.get("savedstream_public") is None


@pytest.mark.asyncio
async def test_admin_review_queue_reconciles_completed_helper_job(access_client) -> None:
    client, database = access_client
    client.cookies.set(ADMIN_COOKIE, signer.issue("admin", "control", 60))
    telegram = app.dependency_overrides[get_telegram]()
    telegram.completed_jobs = [{
        "id": 71,
        "account_id": "alpha",
        "source_chat_id": "100",
        "submitter_telegram_user_id": "100",
        "saved_message_id": 701,
        "status": "completed",
        "updated_at": 7_100,
        "requested_visibility": "public",
        "review_status": "pending",
    }]

    response = await client.get("/api/admin/media/review?status=pending&account=alpha")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [701]
    assert response.json()["items"][0]["review_status"] == "pending"


@pytest.mark.asyncio
async def test_admin_can_delete_violating_media_and_ban_submitter(access_client) -> None:
    client, database = access_client
    client.cookies.set(ADMIN_COOKIE, signer.issue("admin", "control", 60))
    await database.upsert_media_index(
        {
            "account_id": "alpha",
            "id": 701,
            "kind": "video",
            "mime_type": "video/mp4",
            "size": 4,
            "filename": "bad.mp4",
            "original_title": "Bad",
            "caption": "Bad",
            "date": "2026-08-19T00:00:00+00:00",
            "has_thumbnail": True,
        },
        visibility="private",
        source_ingest_job_id=71,
        submitter_telegram_user_id="100",
        requested_visibility="public",
        review_status="pending",
    )
    telegram = app.dependency_overrides[get_telegram]()

    response = await client.post(
        "/api/admin/media/701/review?account=alpha",
        json={
            "decision": "deleted",
            "reason": "违法或危险内容",
            "ban_submitter": True,
            "items": [],
        },
    )

    assert response.status_code == 200
    assert response.json()["deleted"] == 1
    assert telegram.deleted_jobs == [71]
    assert telegram.binding_updates and telegram.binding_updates[-1]["banned"] is True
    deleted = await database.get_media_index("alpha", 701, include_deleted=True, include_provenance=True)
    assert deleted and deleted["deleted"] is True and deleted["size"] == 0
    assert await database.list_media_reviews(status="pending") == []
    banned = await database.get_media_user("100")
    assert banned and banned["status"] == "disabled"


@pytest.mark.asyncio
async def test_media_binary_endpoints_enforce_index_visibility_and_allow_admin_private_access(access_client) -> None:
    client, database = access_client
    await database.upsert_media_index({
        "account_id": "alpha",
        "id": 1,
        "kind": "image",
        "mime_type": "image/jpeg",
        "size": 4,
        "filename": "public.jpg",
        "original_title": "Public",
        "caption": "Public",
        "date": "2026-08-01T00:00:00+00:00",
        "has_thumbnail": True,
    }, visibility="public")
    await database.upsert_media_index({
        "account_id": "alpha",
        "id": 2,
        "kind": "image",
        "mime_type": "image/jpeg",
        "size": 4,
        "filename": "private.jpg",
        "original_title": "Private",
        "caption": "Private",
        "date": "2026-08-01T00:00:00+00:00",
        "has_thumbnail": True,
    }, visibility="private")
    await client.post("/api/access/telegram", json={"code": "valid-code-alpha-123456"})
    await database.set_media_user_status("100", "approved")
    await enable_public_access(database, client)

    public_thumbnail = await client.get("/api/media/1/thumbnail?account=alpha&size=4")
    assert public_thumbnail.status_code == 200
    public_stream = await client.get("/api/media/1/stream?account=alpha")
    assert public_stream.status_code == 200
    assert public_stream.content == b"data"
    private_thumbnail = await client.get("/api/media/2/thumbnail?account=alpha&size=4")
    assert private_thumbnail.status_code == 404

    client.cookies.set(ADMIN_COOKIE, signer.issue("admin", "control", 60))
    admin_thumbnail = await client.get("/api/media/2/thumbnail?account=alpha&size=4")
    assert admin_thumbnail.status_code == 200

    single = await client.patch(
        "/api/admin/media/2/visibility?account=alpha",
        json={"visibility": "public"},
    )
    assert single.status_code == 200
    bulk = await client.post(
        "/api/admin/media/visibility",
        json={
            "visibility": "private",
            "items": [
                {"account_id": "alpha", "message_id": 1},
                {"account_id": "alpha", "message_id": 2},
            ],
        },
    )
    assert bulk.status_code == 200 and bulk.json()["updated"] == 2
    assert (await database.get_media_index("alpha", 1))["visibility"] == "private"
    assert (await database.get_media_index("alpha", 2))["visibility"] == "private"


@pytest.mark.asyncio
async def test_admin_traffic_dashboard_api_and_monthly_limit(access_client) -> None:
    client, database = access_client
    client.cookies.set(ADMIN_COOKIE, signer.issue("admin", "control", 60))

    summary = await client.get("/api/admin/traffic/summary")
    assert summary.status_code == 200
    assert summary.json()["settings"]["monthly_limit_gb"] == 900

    updated = await client.put(
        "/api/admin/traffic/settings",
        json={
            "enabled": True,
            "monthly_capacity_gb": 1,
            "monthly_limit_gb": 0.000000003,
            "warning_percent": 80,
            "admin_bypass": False,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["enabled"] is True

    await database.upsert_media_index(
        {
            "account_id": "alpha",
            "id": 10,
            "kind": "image",
            "mime_type": "image/jpeg",
            "size": 4,
            "filename": "quota.jpg",
            "original_title": "Quota",
            "caption": "Quota",
            "date": "2026-08-01T00:00:00+00:00",
            "has_thumbnail": True,
        },
        visibility="private",
    )
    blocked = await client.get("/api/media/10/stream?account=alpha")
    assert blocked.status_code == 509
    assert blocked.json()["code"] == "TRAFFIC_LIMIT_REACHED"

    series = await client.get("/api/admin/traffic/series?range=7d")
    assert series.status_code == 200
    assert len(series.json()["items"]) == 7

    reset = await client.post("/api/admin/traffic/reset?scope=month")
    assert reset.status_code == 200

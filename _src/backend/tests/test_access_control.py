from __future__ import annotations

from pathlib import Path

import httpx
import aiosqlite
import pytest
import pytest_asyncio

from app.database import Database
from app.main import ADMIN_COOKIE, VIEWER_COOKIE, app, get_database, get_telegram, signer
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


@pytest_asyncio.fixture
async def access_client(tmp_path: Path):
    database = Database(tmp_path / "access.db")
    await database.initialize()
    telegram = FakeTeleBox()
    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_telegram] = lambda: telegram
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
    accounts = await client.get("/api/accounts")
    assert [item["id"] for item in accounts.json()["items"]] == ["alpha"]
    allowed = await client.get("/api/media?account=alpha")
    assert allowed.status_code == 200
    cross_account = await client.get("/api/media?account=beta")
    assert cross_account.status_code == 403


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
    assert (await client.get("/api/media?account=alpha")).status_code == 200

    await database.set_media_user_status("100", "disabled")
    assert (await client.get("/api/media?account=alpha")).status_code == 401


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

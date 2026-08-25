from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.auth import AuthStore
from app.database import Database
from app.main import ADMIN_COOKIE, app, get_auth, get_database, get_telegram, signer, _hash_public_key


class FakeTeleBox:
    def __init__(self, bindings: list[dict] | None = None) -> None:
        self.binding_items = bindings or []

    async def helper_bot_status(self) -> dict:
        return {"configured": True, "username": "savedstream_bot", "token": "masked"}

    async def status(self) -> dict:
        return {"authenticated": True, "state": "authenticated", "error": None}

    async def bindings(self) -> dict:
        return {"items": self.binding_items}

    async def accounts(self) -> dict:
        return {"items": [{"id": "alpha", "label": "Alpha", "state": "authenticated"}]}

    async def set_binding_status(self, telegram_user_id: str, *, enabled: bool, banned: bool = False, reason: str | None = None) -> dict:
        for item in self.binding_items:
            if str(item.get("telegram_user_id")) == str(telegram_user_id):
                item["enabled"] = 1 if enabled else 0
                item["banned"] = 1 if banned else 0
                return item
        return {"telegram_user_id": telegram_user_id, "enabled": 0, "banned": 1 if banned else 0}


@pytest.mark.asyncio
async def test_registration_and_account_login_replace_legacy_telegram_code_flow(tmp_path: Path) -> None:
    database = Database(tmp_path / "onboarding.db")
    await database.initialize()
    await database.set_setting("registration_key_hash", _hash_public_key("invite-key"))
    await database.set_setting("public_registration_enabled", "1")
    auth = AuthStore(database.path)
    telegram = FakeTeleBox()

    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_auth] = lambda: auth
    app.dependency_overrides[get_telegram] = lambda: telegram
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            started = await client.post(
                "/api/auth/register/start",
                json={
                    "username": "alice",
                    "password": "correct-horse-battery",
                    "registration_key": "invite-key",
                    "trust_device": True,
                },
            )
            assert started.status_code == 200
            challenge_id = started.json()["challenge_id"]
            assert started.json()["telegram_bot_link"].startswith("https://t.me/savedstream_bot?start=")

            await auth.claim_challenge(challenge_id, "100", "alice_tg", "Alice")
            registered = await client.get(
                "/api/auth/register/status", params={"challenge_id": challenge_id}
            )
            assert registered.status_code == 200
            assert registered.json()["status"] == "bound"

            logged_in = await client.post(
                "/api/auth/login",
                headers={"X-SavedStream-Browser-ID": "browser-alice"},
                json={
                    "username": "alice",
                    "password": "correct-horse-battery",
                    "trust_device": True,
                },
            )
            assert logged_in.status_code == 200
            assert logged_in.json()["requires_device"] is False

            status = (await client.get("/api/status")).json()
            assert status["access_status"] == "pending"
            assert status["registration_enabled"] is True
            assert status["registration_requires_approval"] is True
            assert status["binding_sync_status"] == "pending"

            assert (await client.post("/api/access/telegram", json={"code": "retired-code-123456"})).status_code == 410
            assert (await client.post("/api/public/login", json={"key": "old-key"})).status_code == 410
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_optional_approval_auto_approves_only_after_valid_account_binding(tmp_path: Path) -> None:
    database = Database(tmp_path / "auto-approval.db")
    await database.initialize()
    await database.set_setting("registration_key_hash", _hash_public_key("invite-key"))
    await database.set_setting("public_registration_enabled", "1")
    await database.set_setting("registration_requires_approval", "0")
    await database.set_setting("public_album_enabled", "1")
    auth = AuthStore(database.path)
    telegram = FakeTeleBox([{
        "telegram_user_id": "100",
        "account_id": "alpha",
        "enabled": 1,
        "banned": 0,
    }])

    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_auth] = lambda: auth
    app.dependency_overrides[get_telegram] = lambda: telegram
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            started = await client.post(
                "/api/auth/register/start",
                headers={"X-SavedStream-Browser-ID": "browser-alice"},
                json={
                    "username": "alice",
                    "password": "correct-horse-battery",
                    "registration_key": "invite-key",
                    "trust_device": True,
                },
            )
            challenge_id = started.json()["challenge_id"]
            await auth.claim_challenge(challenge_id, "100", "alice_tg", "Alice")

            logged_in = await client.post(
                "/api/auth/login",
                headers={"X-SavedStream-Browser-ID": "browser-alice"},
                json={"username": "alice", "password": "correct-horse-battery", "trust_device": True},
            )
            assert logged_in.status_code == 200
            assert logged_in.json()["requires_device"] is False
            assert logged_in.json()["user"]["status"] == "approved"
            assert logged_in.json()["user"]["account_id"] == "alpha"
            assert logged_in.json()["user"]["binding_sync_status"] == "ready"

            status = (await client.get("/api/status")).json()
            assert status["registration_requires_approval"] is False
            assert status["media_authenticated"] is True
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_optional_approval_does_not_open_access_without_account_binding(tmp_path: Path) -> None:
    database = Database(tmp_path / "binding-required.db")
    await database.initialize()
    await database.set_setting("registration_requires_approval", "0")
    auth = AuthStore(database.path)
    telegram = FakeTeleBox([])
    challenge_id, _ = await auth.register_challenge("alice", "correct-horse-battery")
    await auth.claim_challenge(challenge_id, "100", "alice_tg", "Alice")

    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_auth] = lambda: auth
    app.dependency_overrides[get_telegram] = lambda: telegram
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            logged_in = await client.post(
                "/api/auth/login",
                headers={"X-SavedStream-Browser-ID": "browser-alice"},
                json={"username": "alice", "password": "correct-horse-battery"},
            )
            assert logged_in.status_code == 200
            assert logged_in.json()["user"]["status"] == "pending"
            assert logged_in.json()["user"]["binding_sync_status"] == "pending"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_admin_can_toggle_approval_policy_and_review_auth_users(tmp_path: Path) -> None:
    database = Database(tmp_path / "admin-policy.db")
    await database.initialize()
    auth = AuthStore(database.path)
    telegram = FakeTeleBox([{
        "telegram_user_id": "100",
        "account_id": "alpha",
        "enabled": 1,
        "banned": 0,
    }])
    challenge_id, _ = await auth.register_challenge("alice", "correct-horse-battery")
    await auth.claim_challenge(challenge_id, "100", "alice_tg", "Alice")
    user = await auth.get_user_by_username("alice")
    assert user

    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_auth] = lambda: auth
    app.dependency_overrides[get_telegram] = lambda: telegram
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            client.cookies.set(ADMIN_COOKIE, signer.issue("admin", "control", 60))
            toggled = await client.put(
                "/api/admin/public-album",
                json={"registration_requires_approval": False},
            )
            assert toggled.status_code == 200
            assert toggled.json()["registration_requires_approval"] is False
            assert await database.get_setting("registration_requires_approval", "1") == "0"
            auto_approved = await auth.get_user(int(user["id"]))
            assert auto_approved and auto_approved["status"] == "approved"
            assert auto_approved["binding_sync_status"] == "ready"

            disabled = await client.put(f"/api/admin/users/{user['id']}", json={"status": "disabled"})
            assert disabled.status_code == 200
            assert disabled.json()["status"] == "disabled"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_admin_can_choose_random_or_custom_registration_key(tmp_path: Path) -> None:
    database = Database(tmp_path / "registration-key-choice.db")
    await database.initialize()
    auth = AuthStore(database.path)
    telegram = FakeTeleBox()

    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_auth] = lambda: auth
    app.dependency_overrides[get_telegram] = lambda: telegram
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            client.cookies.set(ADMIN_COOKIE, signer.issue("admin", "control", 60))

            random_key = await client.post(
                "/api/admin/public-album/registration-key",
                json={"generate": True},
            )
            assert random_key.status_code == 200
            assert random_key.json()["generated"] is True
            assert len(random_key.json()["key"]) >= 32

            custom_value = "family-access-2026"
            custom_key = await client.post(
                "/api/admin/public-album/registration-key",
                json={"generate": False, "key": custom_value},
            )
            assert custom_key.status_code == 200
            assert custom_key.json()["generated"] is False
            assert custom_key.json()["key"] == custom_value
            assert custom_key.json()["key_version"] == random_key.json()["key_version"] + 1
            assert custom_key.json()["fingerprint"] != random_key.json()["fingerprint"]
            assert await database.get_setting("public_registration_enabled", "1") == "0"

            missing = await client.post(
                "/api/admin/public-album/registration-key",
                json={"generate": False},
            )
            assert missing.status_code == 422
            assert missing.json()["detail"]["code"] == "REGISTRATION_KEY_REQUIRED"

            await database.set_setting("public_registration_enabled", "1")
            accepted = await client.post(
                "/api/auth/register/start",
                json={
                    "username": "customkeyuser",
                    "password": "correct-horse-battery",
                    "registration_key": custom_value,
                },
            )
            assert accepted.status_code == 200
    finally:
        app.dependency_overrides.clear()

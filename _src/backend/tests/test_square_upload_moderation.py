from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

import app.main as main_module
from app.auth import AuthStore
from app.cache import DiskCache
from app.database import Database
from app.main import (
    AccessPrincipal,
    app,
    get_auth,
    get_cache,
    get_database,
    get_indexer,
    get_telegram,
    get_traffic,
    optional_access_principal,
    upload_tasks,
)
from app.traffic import TrafficController


class FakeTeleBox:
    def __init__(self) -> None:
        self.next_message_id = 1000
        self.reserved: list[dict] = []
        self.completed_reservations: list[str] = []
        self.released_reservations: list[str] = []
        self.deleted_media: list[tuple[str, int]] = []
        self.cancelled_users: list[str] = []

    async def accounts(self) -> dict:
        return {
            "items": [
                {"id": "alpha", "label": "Alpha", "state": "authenticated"},
                {"id": "beta", "label": "Beta", "state": "authenticated"},
            ]
        }

    async def resolve_account(self, account_id: str | None) -> str:
        requested = str(account_id or "alpha")
        if requested not in {"alpha", "beta"}:
            raise RuntimeError("account not found")
        return requested

    async def reserve_upload_quota(self, **payload) -> dict:
        key = f"reservation-{len(self.reserved) + 1}"
        self.reserved.append({**payload, "reservation_key": key})
        return {"reservation_key": key}

    async def complete_upload_quota(self, reservation_key: str) -> dict:
        self.completed_reservations.append(reservation_key)
        return {"ok": True}

    async def release_upload_quota(self, reservation_key: str) -> dict:
        self.released_reservations.append(reservation_key)
        return {"ok": True}

    async def upload_file(self, *, account_id: str, file_path: Path, filename: str, mime_type: str, progress_callback=None) -> dict:
        data = file_path.read_bytes()
        if progress_callback:
            result = progress_callback(len(data), len(data))
            if asyncio.iscoroutine(result):
                await result
        self.next_message_id += 1
        return {
            "account_id": account_id,
            "id": self.next_message_id,
            "kind": "file",
            "mime_type": mime_type,
            "size": len(data),
            "filename": filename,
            "original_title": filename,
            "caption": "",
            "date": "2026-08-25T12:00:00+00:00",
            "has_thumbnail": False,
        }

    async def delete_media(self, account_id: str, message_id: int, **_kwargs) -> dict:
        self.deleted_media.append((account_id, int(message_id)))
        return {"ok": True}

    async def cancel_user_ingest_jobs(self, telegram_user_id: str, **_kwargs) -> dict:
        self.cancelled_users.append(str(telegram_user_id))
        return {"cancelled": 0}

    @staticmethod
    def media_cache_key(message: dict, item: dict) -> str:
        return f"{message['account_id']}:{message['id']}:{item.get('size', 0)}"


class DummyIndexer:
    def __init__(self) -> None:
        self.scheduled: list[str] = []

    def schedule_sync(self, account_id: str, *, full: bool = False) -> bool:
        self.scheduled.append(account_id)
        return True


async def create_user(
    auth: AuthStore,
    username: str,
    telegram_user_id: str,
    *,
    role: str = "user",
    account_id: str = "alpha",
) -> dict:
    token, _ = await auth.register_challenge(username, "correct-horse-battery")
    await auth.claim_challenge(token, telegram_user_id, username, username.title())
    user = await auth.get_user_by_username(username)
    assert user
    updated = await auth.update_user(
        int(user["id"]),
        status="approved",
        role=role,
        account_id=account_id,
        binding_sync_status="ready",
    )
    assert updated
    return updated


def principal_for(user: dict) -> AccessPrincipal:
    return AccessPrincipal(
        is_admin=str(user.get("role")) in {"admin", "superadmin"},
        user_id=int(user["id"]),
        username=str(user.get("username_display") or user.get("username_normalized") or ""),
        role=str(user.get("role") or "user"),
        telegram_user_id=str(user.get("telegram_user_id") or "") or None,
        account_id=str(user.get("account_id") or "") or None,
        user_status=str(user.get("status") or "approved"),
        binding_sync_status=str(user.get("binding_sync_status") or "ready"),
        public_authenticated=True,
    )


def media(message_id: int, *, owner_user_id: int | None, telegram_user_id: str | None, visibility: str, requested: str, review: str, hidden: bool = False, source: str = "web") -> dict:
    return {
        "account_id": "alpha",
        "id": message_id,
        "kind": "file",
        "mime_type": "application/octet-stream",
        "size": message_id + 10,
        "filename": f"file-{message_id}.bin",
        "original_title": f"File {message_id}",
        "caption": "",
        "date": "2026-08-25T10:00:00+00:00",
        "has_thumbnail": False,
        "visibility": visibility,
        "requested_visibility": requested,
        "review_status": review,
        "owner_user_id": owner_user_id,
        "submitter_telegram_user_id": telegram_user_id,
        "upload_source": source,
        "hidden": hidden,
    }


@pytest_asyncio.fixture
async def feature_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database = Database(tmp_path / "feature.db")
    await database.initialize()
    await database.set_setting("public_album_enabled", "1")
    auth = AuthStore(database.path)
    telegram = FakeTeleBox()
    indexer = DummyIndexer()
    traffic = TrafficController(database)
    cache = DiskCache(tmp_path / "cache", database.get_cache_limit, "00" * 32)
    await cache.initialize()
    monkeypatch.setattr(main_module, "settings", replace(main_module.settings, data_dir=tmp_path / "data"))
    current: dict[str, AccessPrincipal | None] = {"principal": None}
    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_auth] = lambda: auth
    app.dependency_overrides[get_telegram] = lambda: telegram
    app.dependency_overrides[get_indexer] = lambda: indexer
    app.dependency_overrides[get_traffic] = lambda: traffic
    app.dependency_overrides[get_cache] = lambda: cache
    app.dependency_overrides[optional_access_principal] = lambda: current["principal"]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client, database, auth, telegram, current
    tasks = list(upload_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    upload_tasks.clear()
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_four_media_views_are_strict_and_square_hides_moderation_fields(feature_api) -> None:
    _client, database, auth, _telegram, _current = feature_api
    owner = await create_user(auth, "owner", "100")
    other = await create_user(auth, "other", "200")
    for item in [
        media(1, owner_user_id=owner["id"], telegram_user_id="100", visibility="private", requested="private", review="not_required"),
        media(2, owner_user_id=owner["id"], telegram_user_id="100", visibility="private", requested="public", review="pending"),
        media(3, owner_user_id=owner["id"], telegram_user_id="100", visibility="private", requested="public", review="rejected"),
        media(4, owner_user_id=other["id"], telegram_user_id="200", visibility="public", requested="public", review="approved"),
        media(5, owner_user_id=owner["id"], telegram_user_id="100", visibility="public", requested="public", review="approved"),
        media(6, owner_user_id=other["id"], telegram_user_id="200", visibility="public", requested="public", review="approved", hidden=True),
    ]:
        await database.upsert_media_index(
            item,
            visibility=item["visibility"],
            owner_user_id=item["owner_user_id"],
            submitter_telegram_user_id=item["submitter_telegram_user_id"],
            requested_visibility=item["requested_visibility"],
            review_status=item["review_status"],
            upload_source=item["upload_source"],
            hidden=item["hidden"],
        )

    async def ids(collection: str) -> tuple[list[int], list[dict]]:
        items, _, _ = await database.list_media_index(
            account_id=None,
            limit=50,
            cursor=None,
            order="oldest",
            kind="all",
            query="",
            visibility="all",
            owner_telegram_user_id="100",
            owner_user_id=int(owner["id"]),
            collection=collection,
            viewer_user_id=int(owner["id"]),
            include_provenance=collection == "my_public",
        )
        return [item["id"] for item in items], items

    assert (await ids("private"))[0] == [1]
    assert (await ids("my_public"))[0] == [2, 3, 5]
    square_ids, square = await ids("square")
    assert square_ids == [4, 5]
    assert square[1]["owned_by_me"] is True
    for internal in ("owner_user_id", "submitter_telegram_user_id", "review_status", "review_reason", "review_batch_id", "upload_source"):
        assert internal not in square[0]

    await database.set_media_like(int(owner["id"]), "alpha", 4, True)
    assert (await ids("liked"))[0] == [4]
    await database.set_media_hidden("alpha", 4, True)
    assert (await ids("liked"))[0] == []


@pytest.mark.asyncio
async def test_like_and_report_rules_are_idempotent_and_reject_self_actions(feature_api) -> None:
    client, database, auth, _telegram, current = feature_api
    owner = await create_user(auth, "owner2", "101")
    reporter = await create_user(auth, "reporter", "201")
    await database.upsert_media_index(
        media(20, owner_user_id=owner["id"], telegram_user_id="101", visibility="public", requested="public", review="approved"),
        visibility="public",
        owner_user_id=int(owner["id"]),
        submitter_telegram_user_id="101",
        requested_visibility="public",
        review_status="approved",
        upload_source="web",
    )

    current["principal"] = principal_for(reporter)
    first = await client.put("/api/media/20/like?account=alpha")
    second = await client.put("/api/media/20/like?account=alpha")
    assert first.status_code == second.status_code == 200
    assert first.json()["like_count"] == second.json()["like_count"] == 1
    report = await client.post("/api/media/20/reports?account=alpha", json={"reason_code": "malware", "details": "suspicious"})
    assert report.status_code == 201
    await database.resolve_media_reports("alpha", 20, status="failed", action="delete", reason="telegram offline", resolved_by=None)
    duplicate = await client.post("/api/media/20/reports?account=alpha", json={"reason_code": "spam", "details": None})
    assert duplicate.status_code == 409
    await database.create_user_sanction(
        user_id=int(reporter["id"]),
        sanction_type="report_mute",
        reason="malicious reports",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        created_by=None,
    )
    muted = await client.post("/api/media/20/reports?account=alpha", json={"reason_code": "spam", "details": None})
    assert muted.status_code == 403
    assert muted.json()["detail"]["code"] == "REPORTING_DISABLED"
    assert muted.json()["detail"]["reason"] == "malicious reports"

    await database.create_user_sanction(
        user_id=int(reporter["id"]),
        sanction_type="login_ban",
        reason="account review",
        expires_at=None,
        created_by=None,
    )
    login = await client.post(
        "/api/auth/login",
        headers={"X-SavedStream-Browser-ID": "new-browser"},
        json={"username": "reporter", "password": "correct-horse-battery", "trust_device": False},
    )
    assert login.status_code == 403
    assert login.json()["detail"]["code"] == "LOGIN_BANNED"
    assert login.json()["detail"]["permanent"] is True

    current["principal"] = principal_for(owner)
    self_like = await client.put("/api/media/20/like?account=alpha")
    self_report = await client.post("/api/media/20/reports?account=alpha", json={"reason_code": "other", "details": None})
    assert self_like.status_code == 409
    assert self_like.json()["detail"]["code"] == "SELF_LIKE_FORBIDDEN"
    assert self_report.status_code == 409
    assert self_report.json()["detail"]["code"] == "SELF_REPORT_FORBIDDEN"


async def wait_for_upload(database: Database, job_id: str) -> dict:
    for _ in range(100):
        job = await database.get_upload_job(job_id)
        if job and job["status"] in {"completed", "failed", "cancelled"}:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("upload did not finish")


def encoded_filename(filename: str) -> str:
    return base64.urlsafe_b64encode(filename.encode("utf-8")).decode("ascii").rstrip("=")


@pytest.mark.asyncio
async def test_web_upload_preserves_filename_owner_batch_and_task_isolation(feature_api) -> None:
    client, database, auth, telegram, current = feature_api
    owner = await create_user(auth, "uploader", "300")
    stranger = await create_user(auth, "stranger", "301")
    current["principal"] = principal_for(owner)
    headers = {
        "Content-Type": "application/octet-stream",
        "X-Upload-Filename": encoded_filename("示例 文件.bin"),
        "X-Upload-Mime": "application/octet-stream",
        "X-Upload-Batch-ID": "batch-shared",
    }
    created = await client.post("/api/uploads?visibility=public", content=b"first", headers=headers)
    assert created.status_code == 202
    assert created.json()["filename"] == "示例 文件.bin"
    assert "quota_reservation_key" not in created.json()
    job = await wait_for_upload(database, created.json()["id"])
    assert job["review_status"] == "pending"
    indexed = await database.get_media_index("alpha", int(job["message_id"]), include_provenance=True)
    assert indexed
    assert indexed["filename"] == "示例 文件.bin"
    assert indexed["owner_user_id"] == owner["id"]
    assert indexed["visibility"] == "private"
    assert indexed["requested_visibility"] == "public"
    assert indexed["upload_batch_id"] == "batch-shared"
    assert telegram.completed_reservations == ["reservation-1"]

    current["principal"] = principal_for(stranger)
    hidden_job = await client.get(f"/api/uploads/{job['id']}")
    assert hidden_job.status_code == 404
    cross_account = await client.post("/api/uploads?visibility=private&account=beta", content=b"x", headers={**headers, "Content-Length": "1"})
    assert cross_account.status_code == 403

    current["principal"] = principal_for(owner)
    mismatch = await client.post(
        "/api/uploads?visibility=private",
        content=b"short",
        headers={**headers, "Content-Length": "12"},
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["detail"]["code"] == "UPLOAD_LENGTH_MISMATCH"
    assert telegram.released_reservations == ["reservation-2"]
    empty = await client.post("/api/uploads?visibility=private", content=b"", headers={**headers, "Content-Length": "0"})
    assert empty.status_code == 411


@pytest.mark.asyncio
async def test_folder_upload_is_assigned_and_hidden_from_root_until_search(feature_api) -> None:
    client, database, auth, _telegram, current = feature_api
    owner = await create_user(auth, "folderowner", "350")
    current["principal"] = principal_for(owner)
    folder = await database.create_folder("项目文件", parent_id=0)
    headers = {
        "Content-Type": "application/octet-stream",
        "X-Upload-Filename": encoded_filename("inside-folder.bin"),
        "Content-Length": "6",
    }
    created = await client.post(
        f"/api/uploads?visibility=private&folder_id={folder['id']}",
        content=b"folder",
        headers=headers,
    )
    assert created.status_code == 202
    job = await wait_for_upload(database, created.json()["id"])
    assert int(job["folder_id"]) == int(folder["id"])

    root = await client.get("/api/media?view=private")
    assert root.status_code == 200
    assert all(item["id"] != int(job["message_id"]) for item in root.json()["items"])

    folder_page = await client.get(f"/api/media?view=private&folder_id={folder['id']}")
    assert folder_page.status_code == 200
    assert [item["id"] for item in folder_page.json()["items"]] == [int(job["message_id"])]

    search = await client.get("/api/media?view=private&q=inside-folder")
    assert search.status_code == 200
    assert [item["id"] for item in search.json()["items"]] == [int(job["message_id"])]


@pytest.mark.asyncio
async def test_admin_public_upload_is_direct_and_named_admin_sanctions_still_apply(feature_api) -> None:
    client, database, auth, telegram, current = feature_api
    admin = await create_user(auth, "namedadmin", "400", role="admin")
    current["principal"] = principal_for(admin)
    headers = {
        "Content-Type": "application/octet-stream",
        "X-Upload-Filename": encoded_filename("admin.bin"),
        "Content-Length": "5",
    }
    created = await client.post("/api/uploads?visibility=public&account=beta", content=b"admin", headers=headers)
    assert created.status_code == 202
    job = await wait_for_upload(database, created.json()["id"])
    assert job["account_id"] == "alpha"
    indexed = await database.get_media_index("alpha", int(job["message_id"]), include_provenance=True)
    assert indexed and indexed["visibility"] == "public" and indexed["review_status"] == "approved"
    assert telegram.reserved == []

    await database.create_user_sanction(
        user_id=int(admin["id"]),
        sanction_type="upload_mute",
        reason="maintenance",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        created_by=None,
    )
    blocked = await client.post("/api/uploads?visibility=private&account=beta", content=b"again", headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == {
        "code": "UPLOAD_MUTED",
        "sanction_type": "upload_mute",
        "reason": "maintenance",
        "expires_at": blocked.json()["detail"]["expires_at"],
        "permanent": False,
    }


@pytest.mark.asyncio
async def test_content_deletion_only_selects_confirmed_ownership_and_role_protection(feature_api) -> None:
    client, database, auth, _telegram, current = feature_api
    user = await create_user(auth, "contentowner", "500")
    moderator = await create_user(auth, "moderator", "501", role="admin")
    target_admin = await create_user(auth, "targetadmin", "502", role="admin")
    superadmin = await create_user(auth, "supervisor", "503", role="superadmin")
    await database.upsert_media_index(
        media(30, owner_user_id=user["id"], telegram_user_id="500", visibility="private", requested="private", review="not_required", source="web"),
        visibility="private", owner_user_id=int(user["id"]), submitter_telegram_user_id="500", requested_visibility="private", review_status="not_required", upload_source="web",
    )
    await database.upsert_media_index(
        media(31, owner_user_id=user["id"], telegram_user_id="500", visibility="private", requested="private", review="not_required", source="legacy"),
        visibility="private", owner_user_id=int(user["id"]), submitter_telegram_user_id="500", requested_visibility="private", review_status="not_required", upload_source="legacy",
    )
    deletion = await database.create_content_deletion_job(
        job_id="delete-confirmed",
        target_user_id=int(user["id"]),
        telegram_user_id="500",
        reason="cleanup",
        created_by=int(superadmin["id"]),
    )
    assert deletion["total_items"] == 1
    assert deletion["items"][0]["message_id"] == 30

    current["principal"] = principal_for(moderator)
    denied = await client.post(
        f"/api/admin/users/{target_admin['id']}/sanctions",
        json={"user_id": target_admin["id"], "sanctions": [{"sanction_type": "report_mute", "reason": "abuse", "expires_at": None}], "delete_all_content": False},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "SUPERADMIN_REQUIRED"

    current["principal"] = principal_for(superadmin)
    allowed = await client.post(
        f"/api/admin/users/{target_admin['id']}/sanctions",
        json={"user_id": target_admin["id"], "sanctions": [{"sanction_type": "report_mute", "reason": "abuse", "expires_at": None}], "delete_all_content": False},
    )
    assert allowed.status_code == 200

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import uuid
import aiosqlite
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Cookie, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .backups import cleanup_backups, delete_backup, list_backups
from .cache import DiskCache
from .auth import AuthStore, hash_browser_id, validate_password, validate_username
from .config import settings
from .database import (
    DEFAULT_HELPER_GLOBAL_FILES_PER_MINUTE,
    DEFAULT_HELPER_MAX_ALBUM_BYTES,
    DEFAULT_HELPER_MAX_ALBUM_ITEMS,
    DEFAULT_HELPER_MAX_FILE_BYTES,
    DEFAULT_HELPER_PER_USER_BYTES_24H,
    DEFAULT_HELPER_PER_USER_CONCURRENT,
    DEFAULT_HELPER_PER_USER_FILES_24H,
    Database,
)
from .media_indexer import MediaIndexer
from .replication import DisasterRecoveryManager
from .media_crypto import DeviceKeyError, encrypt_for_device, load_device_public_key, parse_device_public_key
from .ranges import InvalidRange, parse_range_header
from .security import TokenSigner, constant_time_equal
from .storage import storage_snapshot, storage_watchdog
from .system_backups import (
    BACKUP_MARKER,
    SystemBackupError,
    archive_size,
    backup_filename,
    create_archive,
    extract_archive,
    next_cron,
    snapshot_sqlite,
    unwrap_passphrase,
    utc_now,
    wrap_passphrase,
)
from .traffic import TrafficController, TrafficLimitExceeded
from .telebox_client import (
    TELEGRAM_CHUNK_SIZE,
    MediaNotFound,
    InvalidWebLoginCode,
    UploadQuotaExceeded,
    TeleBoxClient,
    TelegramUnavailable,
    guess_image_content_type,
)


ADMIN_COOKIE = "savedstream_admin"
VIEWER_COOKIE = "savedstream_viewer"
ACCESS_COOKIE = "savedstream_access"
PUBLIC_COOKIE = "savedstream_public"
DEVICE_COOKIE = "savedstream_device"
AUTH_COOKIE = "savedstream_auth"
BROWSER_ID_HEADER = "X-SavedStream-Browser-ID"
COOKIE_TTL = settings.session_cookie_days * 24 * 60 * 60
signer = TokenSigner(f"{settings.admin_key}:{settings.api_hash}:savedstream")


class KeyPayload(BaseModel):
    key: str = Field(min_length=1, max_length=512)


class UserPasswordPayload(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)
    trust_device: bool = False


class RegisterPayload(UserPasswordPayload):
    registration_key: str = Field(min_length=1, max_length=512)


class AdminBootstrapPayload(UserPasswordPayload):
    admin_key: str = Field(min_length=1, max_length=512)


class AdminUserUpdatePayload(BaseModel):
    status: str | None = Field(default=None, pattern="^(pending|approved|disabled|denied)$")
    role: str | None = Field(default=None, pattern="^(user|admin|superadmin)$")
    account_id: str | None = Field(default=None, max_length=40)
    ban_reason: str | None = Field(default=None, max_length=1000)


class MediaReportPayload(BaseModel):
    reason_code: str = Field(pattern="^(illegal|sexual|copyright|malware|spam|privacy|other)$")
    details: str | None = Field(default=None, max_length=1000)


class SanctionPayload(BaseModel):
    sanction_type: str = Field(pattern="^(upload_mute|login_ban|report_mute)$")
    reason: str = Field(min_length=1, max_length=1000)
    expires_at: str | None = Field(default=None, max_length=64)


class SanctionTargetPayload(BaseModel):
    user_id: int = Field(ge=1)
    sanctions: list[SanctionPayload] = Field(default_factory=list, max_length=3)
    delete_all_content: bool = False


class ReportResolutionPayload(BaseModel):
    resolution: str = Field(pattern="^(actioned|ignored)$")
    media_action: str = Field(default="none", pattern="^(none|private|hidden|delete)$")
    reason: str | None = Field(default=None, max_length=1000)
    targets: list[SanctionTargetPayload] = Field(default_factory=list, max_length=100)


class ContentDeletionPayload(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class AdminCreatePayload(UserPasswordPayload):
    role: str = Field(default="admin", pattern="^(admin|superadmin)$")


class PasswordResetCompletePayload(BaseModel):
    challenge_id: str = Field(min_length=20, max_length=256)
    password: str = Field(min_length=1, max_length=128)


class PublicAlbumSettingsPayload(BaseModel):
    enabled: bool | None = None
    registration_enabled: bool | None = None
    registration_requires_approval: bool | None = None
    registration_key: str | None = Field(default=None, min_length=1, max_length=512)


class RegistrationKeyPayload(BaseModel):
    key: str | None = Field(default=None, min_length=1, max_length=512)
    generate: bool = True


class TelegramChallengeClaimPayload(BaseModel):
    challenge_token: str = Field(min_length=20, max_length=256)
    telegram_user_id: str = Field(min_length=1, max_length=64)
    telegram_username: str | None = Field(default=None, max_length=128)
    display_name: str = Field(default="", max_length=256)
    chat_type: str = Field(default="private", max_length=32)


class FilenameCheckPayload(BaseModel):
    filename: str = Field(min_length=1, max_length=500)


class DeviceKeyPayload(BaseModel):
    device_public_key: str = Field(min_length=300, max_length=4096)
    key_format: str = Field(pattern="^spki-rsa-oaep-v1$")
    persistence: str = Field(default="persistent", pattern="^(persistent|session)$")


class PasswordPayload(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class SettingsPayload(BaseModel):
    cache_max_gb: float = Field(ge=0.5, le=200)
    access_restricted: bool = True
    viewer_key: str | None = Field(default=None, max_length=512)
    clear_viewer_key: bool = False


class TitlePayload(BaseModel):
    title: str = Field(default="", max_length=200)


class HelperBotPayload(BaseModel):
    token: str = Field(min_length=20, max_length=256)


class BindingPayload(BaseModel):
    submitter_id: str = Field(min_length=1, max_length=32)


class AccountPayload(BaseModel):
    id: str = Field(pattern="^[a-zA-Z0-9_-]{1,40}$")
    label: str = Field(min_length=1, max_length=80)
    api_id: int = Field(gt=0)
    api_hash: str = Field(min_length=20, max_length=128)
    session: str = Field(default="", max_length=8192)
    group_id: str | None = Field(default=None, max_length=80)
    role: str = Field(default="primary", pattern="^(primary|replica)$")
    priority: int = Field(default=100, ge=1, le=10000)


class AccountGroupSettingsPayload(BaseModel):
    auto_failover_enabled: bool = True
    replication_enabled: bool = True
    rate_min_interval_ms: int = Field(default=3000, ge=1500, le=600000)
    rate_max_messages_per_minute: int = Field(default=10, ge=1, le=20)
    rate_concurrency: int = Field(default=1, ge=1, le=1)


class ManualFailoverPayload(BaseModel):
    target_account_id: str = Field(min_length=1, max_length=40)


class TelegramAccessPayload(BaseModel):
    code: str = Field(min_length=16, max_length=256)


class AccessUserStatusPayload(BaseModel):
    status: str = Field(pattern="^(approved|disabled|denied)$")


class PublicAlbumPayload(BaseModel):
    enabled: bool


class VisibilityPayload(BaseModel):
    visibility: str = Field(pattern="^(public|private|hidden)$")


class BulkVisibilityItem(BaseModel):
    account_id: str = Field(min_length=1, max_length=40)
    message_id: int = Field(gt=0)


class BulkVisibilityPayload(BaseModel):
    visibility: str = Field(pattern="^(public|private|hidden)$")
    items: list[BulkVisibilityItem] = Field(min_length=1, max_length=1000)


class FolderPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    parent_id: int = Field(default=0, ge=0)


class FolderRenamePayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    parent_id: int | None = Field(default=None, ge=0)


class FolderItemsPayload(BaseModel):
    items: list[BulkVisibilityItem] = Field(min_length=1, max_length=1000)


class NotificationReadPayload(BaseModel):
    ids: list[int] | None = Field(default=None, max_length=500)
    all: bool = False


class NotificationDeletePayload(BaseModel):
    ids: list[int] | None = Field(default=None, max_length=500)
    all: bool = False


class AdminNotificationPayload(BaseModel):
    user_id: int | None = Field(default=None, ge=1)
    kind: str = Field(default="system", max_length=40)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=2000)
    link: str | None = Field(default=None, max_length=500)


class BackupCleanupPayload(BaseModel):
    keep: int = Field(default=3, ge=1, le=20)
    dry_run: bool = False


class SystemBackupSettingsPayload(BaseModel):
    enabled: bool = False
    cron_expr: str = Field(default="0 3 * * *", min_length=1, max_length=120)
    timezone: str = Field(default="UTC", min_length=1, max_length=80)
    account_id: str | None = Field(default=None, max_length=40)
    passphrase: str | None = Field(default=None, min_length=8, max_length=512)
    clear_passphrase: bool = False


class ReviewPayload(BaseModel):
    decision: str = Field(pattern="^(approved|rejected|revoked|deleted)$")
    reason: str | None = Field(default=None, max_length=1000)
    ban_submitter: bool = False


class DeleteMediaPayload(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)
    ban_submitter: bool = False


class BulkReviewItem(BaseModel):
    account_id: str = Field(min_length=1, max_length=40)
    message_id: int = Field(gt=0)


class BulkReviewPayload(BaseModel):
    decision: str = Field(pattern="^(approved|rejected|revoked|deleted)$")
    reason: str | None = Field(default=None, max_length=1000)
    ban_submitter: bool = False
    items: list[BulkReviewItem] = Field(min_length=1, max_length=1000)


class HelperRateLimitPayload(BaseModel):
    per_user_files_24h: int = Field(ge=1, le=100000)
    per_user_bytes_24h: int = Field(ge=1, le=10 * 1024**4)
    per_user_concurrent: int = Field(ge=1, le=100)
    max_file_bytes: int = Field(ge=1, le=10 * 1024**4)
    global_files_per_minute: int = Field(ge=1, le=100000)
    max_album_items: int = Field(ge=1, le=1000)
    max_album_bytes: int = Field(ge=1, le=10 * 1024**4)


class FilenameSensitiveSettingsPayload(BaseModel):
    max_attempts_10m: int = Field(default=10, ge=1, le=1000)
    cooldown_seconds: int = Field(default=30, ge=1, le=3600)


class BindInviteSettingsPayload(BaseModel):
    enabled: bool = True
    global_joins_24h: int = Field(default=100, ge=1, le=1_000_000)
    per_user_generation_24h: int = Field(default=1, ge=1, le=100)


class TrafficSettingsPayload(BaseModel):
    enabled: bool = False
    monthly_capacity_gb: float = Field(gt=0, le=10000)
    monthly_limit_gb: float = Field(gt=0, le=10000)
    warning_percent: int = Field(ge=1, le=99)
    admin_bypass: bool = False


@dataclass(frozen=True)
class AccessPrincipal:
    is_admin: bool
    user_id: int | None = None
    username: str | None = None
    role: str = "user"
    telegram_user_id: str | None = None
    account_id: str | None = None
    user_status: str = "approved"
    binding_sync_status: str = "ready"
    public_authenticated: bool = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    database = Database(settings.database_path)
    await database.initialize()
    auth = AuthStore(settings.database_path)
    cache = DiskCache(settings.cache_dir, database.get_cache_limit, settings.media_cache_key)
    await cache.initialize()
    traffic = TrafficController(database)
    telegram = TeleBoxClient(settings)
    await telegram.initialize()
    indexer = MediaIndexer(database, telegram)
    replication = DisasterRecoveryManager(database, telegram)
    indexer.replication = replication
    app.state.database = database
    app.state.auth = auth
    app.state.cache = cache
    app.state.traffic = traffic
    app.state.telegram = telegram
    app.state.indexer = indexer
    app.state.replication = replication
    await indexer.start()
    await replication.ensure_groups()
    await replication.start()
    system_backup_scheduler = asyncio.create_task(
        _system_backup_scheduler(database, telegram, indexer),
        name="system-backup-scheduler",
    )
    app.state.system_backup_scheduler = system_backup_scheduler
    storage_task = asyncio.create_task(
        storage_watchdog(database),
        name="storage-watchdog",
    )
    yield
    storage_task.cancel()
    try:
        await storage_task
    except asyncio.CancelledError:
        pass
    system_backup_scheduler.cancel()
    try:
        await system_backup_scheduler
    except asyncio.CancelledError:
        pass
    pending_system_backups = list(system_backup_tasks.values())
    for task in pending_system_backups:
        if not task.done():
            task.cancel()
    if pending_system_backups:
        await asyncio.gather(*pending_system_backups, return_exceptions=True)
    system_backup_tasks.clear()
    await replication.stop()
    await indexer.stop()
    pending_uploads = list(upload_tasks.values())
    for task in pending_uploads:
        if not task.done():
            task.cancel()
    if pending_uploads:
        await asyncio.gather(*pending_uploads, return_exceptions=True)
    upload_tasks.clear()
    pending_deletions = list(content_deletion_tasks.values())
    for task in pending_deletions:
        if not task.done():
            task.cancel()
    if pending_deletions:
        await asyncio.gather(*pending_deletions, return_exceptions=True)
    content_deletion_tasks.clear()
    await telegram.close()


app = FastAPI(title="SavedStream", version="0.1.0", lifespan=lifespan)

# Keep a process-local handle for administrator cancellation.  The upload
# state itself remains durable in SQLite, so a restart still exposes the last
# known state and the normal retry path can be used afterwards.
upload_tasks: dict[str, asyncio.Task[None]] = {}
content_deletion_tasks: dict[str, asyncio.Task[None]] = {}
system_backup_tasks: dict[str, asyncio.Task[None]] = {}
system_backup_lock = asyncio.Lock()
system_backup_restore_lock = asyncio.Lock()


def _system_backup_public_settings(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(int(raw.get("enabled") or 0)),
        "cron_expr": str(raw.get("cron_expr") or "0 3 * * *"),
        "timezone": str(raw.get("timezone") or "UTC"),
        "account_id": raw.get("account_id"),
        "passphrase_configured": bool(raw.get("passphrase_ciphertext")),
        "next_run_at": raw.get("next_run_at"),
        "last_run_at": raw.get("last_run_at"),
        "last_status": raw.get("last_status") or "idle",
        "last_error": raw.get("last_error"),
        "updated_at": raw.get("updated_at"),
    }


async def _stored_system_backup_passphrase(database: Database) -> str:
    raw = await database.get_system_backup_settings()
    ciphertext = raw.get("passphrase_ciphertext")
    if not ciphertext:
        raise SystemBackupError("backup passphrase is not configured")
    return unwrap_passphrase(
        {
            "salt": str(raw.get("passphrase_salt") or ""),
            "nonce": str(raw.get("passphrase_nonce") or ""),
            "ciphertext": str(ciphertext),
        },
        settings.admin_key,
    )


def _runtime_config_snapshot() -> bytes:
    keys = (
        "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "ADMIN_KEY", "MEDIA_CACHE_KEY",
        "TELEBOX_API_TOKEN", "SAVEDSTREAM_INTERNAL_TOKEN", "TELEBOX_SECRET_KEY",
        "TELEBOX_DEFAULT_ACCOUNT", "COOKIE_SECURE", "SESSION_COOKIE_DAYS",
    )
    return json.dumps({key: os.getenv(key, "") for key in keys}, ensure_ascii=False, sort_keys=True).encode("utf-8")


async def _notify_system_backup_failure(database: Database, message: str) -> None:
    try:
        auth = AuthStore(database.path)
        admins = [user for user in await auth.list_users() if str(user.get("role")) in {"admin", "superadmin"}]
        for user in admins:
            if user.get("id"):
                await database.create_notification(int(user["id"]), "system_backup", "服务端配置备份失败", message, "/admin")
    except Exception:
        # A backup failure must never crash the main service or scheduler.
        pass


async def _run_system_backup_job(
    job_id: str,
    *,
    trigger: str,
    database: Database,
    telegram: TeleBoxClient,
    indexer: MediaIndexer,
    created_by: int | None = None,
) -> None:
    staging = settings.data_dir / "system-backup-staging" / job_id
    archive_path = staging / backup_filename()
    account_id: str | None = None
    try:
        await database.update_system_backup_job(job_id, status="running", phase="snapshot", progress=5)
        async with system_backup_lock:
            config = await database.get_system_backup_settings()
            account_id = await telegram.resolve_account(str(config.get("account_id") or settings.telebox_default_account))
            passphrase = await _stored_system_backup_passphrase(database)
            staging.mkdir(parents=True, exist_ok=True)
            db_snapshot = staging / "savedstream.db"
            await asyncio.to_thread(snapshot_sqlite, settings.database_path, db_snapshot)
            await database.update_system_backup_job(job_id, phase="telebox_export", progress=25)
            telebox_payload = await telegram.export_system_backup()
            sections = {
                "savedstream.db": db_snapshot.read_bytes(),
                "telebox.json": json.dumps(telebox_payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                "runtime_config.json": _runtime_config_snapshot(),
            }
            await database.update_system_backup_job(job_id, phase="encrypt", progress=45)
            manifest = await asyncio.to_thread(
                create_archive,
                archive_path,
                passphrase=passphrase,
                sections=sections,
                metadata={"application": "SavedStream", "backup_marker": BACKUP_MARKER},
            )
            await database.update_system_backup_job(job_id, status="uploading", phase="telegram_upload", progress=60)
            uploaded = await telegram.upload_file(
                account_id=account_id,
                file_path=archive_path,
                filename=archive_path.name,
                mime_type="application/x-savedstream-backup",
                caption=f"{BACKUP_MARKER}\n{archive_path.name}",
            )
            message_id = int(uploaded.get("id") or uploaded.get("message_id") or 0)
            backup_id = str(uuid.uuid4())
            record = await database.create_system_backup({
                "id": backup_id,
                "filename": archive_path.name,
                "source": trigger,
                "status": "available",
                "created_at": str(manifest.get("created_at") or utc_now()),
                "size_bytes": archive_size(archive_path),
                "sha256": str(manifest.get("archive_sha256") or ""),
                "account_id": account_id,
                "message_id": message_id or None,
                "manifest_json": json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                "error": None,
                "imported_at": None,
            })
            if message_id and uploaded.get("kind"):
                item = {**uploaded, "account_id": account_id}
                await database.upsert_media_index(
                    item,
                    visibility="private",
                    hidden=True,
                    upload_source="system_backup",
                    requested_visibility="private",
                    review_status="not_required",
                )
            await database.update_system_backup_job(job_id, backup_id=record["id"], status="completed", phase="completed", progress=100, completed_at=utc_now())
            await database.update_system_backup_settings({"last_run_at": utc_now(), "last_status": "success", "last_error": None})
    except Exception as exc:
        message = str(exc)
        current_job = await database.get_system_backup_job(job_id)
        attempts = int((current_job or {}).get("attempts") or 0) + 1
        if attempts < 3:
            await database.update_system_backup_job(job_id, status="queued", phase="retry_wait", attempts=attempts, error=message)
            await asyncio.sleep(2 ** attempts)
            await _run_system_backup_job(job_id, trigger=trigger, database=database, telegram=telegram, indexer=indexer, created_by=created_by)
            return
        await database.update_system_backup_job(job_id, status="failed", phase="failed", error=message, completed_at=utc_now())
        await database.update_system_backup_settings({"last_run_at": utc_now(), "last_status": "failed", "last_error": message})
        await _notify_system_backup_failure(database, message)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        system_backup_tasks.pop(job_id, None)


async def _system_backup_scheduler(database: Database, telegram: TeleBoxClient, indexer: MediaIndexer) -> None:
    while True:
        try:
            config = await database.get_system_backup_settings()
            if not bool(int(config.get("enabled") or 0)) or not config.get("passphrase_ciphertext"):
                await asyncio.sleep(30)
                continue
            next_at = config.get("next_run_at")
            now = datetime.now(timezone.utc)
            if not next_at:
                next_value = next_cron(str(config.get("cron_expr") or "0 3 * * *"), str(config.get("timezone") or "UTC"), now)
                await database.update_system_backup_settings({"next_run_at": next_value.isoformat()})
                await asyncio.sleep(1)
                continue
            try:
                due = datetime.fromisoformat(str(next_at).replace("Z", "+00:00"))
            except ValueError:
                due = now
            delay = (due - now).total_seconds()
            if delay > 0:
                await asyncio.sleep(min(delay, 30))
                continue
            if system_backup_tasks:
                next_value = next_cron(str(config.get("cron_expr") or "0 3 * * *"), str(config.get("timezone") or "UTC"), now)
                await database.update_system_backup_settings({"next_run_at": next_value.isoformat()})
                await asyncio.sleep(5)
                continue
            job_id = str(uuid.uuid4())
            await database.create_system_backup_job({
                "id": job_id, "backup_id": None, "trigger": "scheduled", "status": "queued", "phase": "queued",
                "progress": 0, "attempts": 0, "temp_path": None, "error": None, "created_by": None,
                "created_at": utc_now(), "updated_at": utc_now(), "completed_at": None,
            })
            task = asyncio.create_task(_run_system_backup_job(job_id, trigger="scheduled", database=database, telegram=telegram, indexer=indexer), name=f"system-backup-{job_id}")
            system_backup_tasks[job_id] = task
            next_value = next_cron(str(config.get("cron_expr") or "0 3 * * *"), str(config.get("timezone") or "UTC"), now)
            await database.update_system_backup_settings({"next_run_at": next_value.isoformat()})
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(30)


async def _copy_upload_to_path(upload: UploadFile, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with destination.open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 10 * 1024**3:
                raise HTTPException(status_code=413, detail={"code": "BACKUP_TOO_LARGE"})
            handle.write(chunk)
    await upload.close()
    return total


async def _restore_system_backup_archive(
    job_id: str,
    archive_path: Path,
    *,
    database: Database,
    telegram: TeleBoxClient,
    indexer: MediaIndexer,
    passphrase: str,
    backup_id: str | None = None,
) -> None:
    work = settings.data_dir / "system-backup-restore" / job_id
    rollback_db = work / "rollback.db"
    try:
        await database.update_system_backup_job(job_id, status="validating", phase="validating", progress=10)
        work.mkdir(parents=True, exist_ok=True)
        manifest = await asyncio.to_thread(extract_archive, archive_path, passphrase, work / "extracted")
        restored_db = work / "extracted" / "savedstream.db"
        telebox_file = work / "extracted" / "telebox.json"
        if not restored_db.is_file():
            raise SystemBackupError("backup is missing savedstream.db")
        await asyncio.to_thread(snapshot_sqlite, settings.database_path, rollback_db)
        previous_telebox = await telegram.export_system_backup() if telebox_file.is_file() else None
        await database.update_system_backup_job(job_id, status="restoring", phase="pause_indexer", progress=35)
        async with system_backup_restore_lock:
            try:
                pending_uploads = list(upload_tasks.values())
                for pending in pending_uploads:
                    if not pending.done():
                        pending.cancel()
                if pending_uploads:
                    await asyncio.gather(*pending_uploads, return_exceptions=True)
                upload_tasks.clear()
                if hasattr(indexer, "stop"):
                    await indexer.stop()
                await database.update_system_backup_job(job_id, phase="telebox_import", progress=50)
                if telebox_file.is_file():
                    await telegram.import_system_backup(json.loads(telebox_file.read_text(encoding="utf-8")))
                await database.update_system_backup_job(job_id, phase="database_replace", progress=70)
                os.replace(restored_db, settings.database_path)
                await database.initialize()
                if hasattr(indexer, "start"):
                    await indexer.start()
            except Exception:
                if previous_telebox is not None:
                    try:
                        await telegram.import_system_backup(previous_telebox)
                    except Exception:
                        pass
                if rollback_db.exists():
                    os.replace(rollback_db, settings.database_path)
                await database.initialize()
                if hasattr(indexer, "start"):
                    await indexer.start()
                raise
        if backup_id:
            await database.update_system_backup(
                backup_id,
                status="available",
                size_bytes=archive_size(archive_path),
                sha256=str(manifest.get("archive_sha256") or ""),
                manifest_json=json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                error=None,
                imported_at=utc_now(),
            )
        await database.update_system_backup_job(job_id, status="completed", phase="completed", progress=100, completed_at=utc_now(), error=None)
    except Exception as exc:
        await database.update_system_backup_job(job_id, status="failed", phase="failed", error=str(exc), completed_at=utc_now())
    finally:
        shutil.rmtree(work, ignore_errors=True)
        archive_path.unlink(missing_ok=True)
        try:
            archive_path.parent.rmdir()
        except OSError:
            pass
        system_backup_tasks.pop(job_id, None)


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_auth(request: Request) -> AuthStore:
    auth = getattr(request.app.state, "auth", None)
    if auth is not None:
        return auth
    database = getattr(request.app.state, "database", None)
    if database is None:
        override = request.app.dependency_overrides.get(get_database)
        database = override() if override else None
    if database is None:
        raise HTTPException(status_code=503, detail={"code": "AUTH_UNAVAILABLE"})
    auth = AuthStore(database.path)
    request.app.state.auth = auth
    return auth


def get_cache(request: Request) -> DiskCache:
    return request.app.state.cache


def get_telegram(request: Request) -> TeleBoxClient:
    return request.app.state.telegram


def get_traffic(
    request: Request,
    database: Database = Depends(get_database),
) -> TrafficController:
    traffic = getattr(request.app.state, "traffic", None)
    if traffic is None or traffic.database.path != database.path:
        traffic = TrafficController(database)
        request.app.state.traffic = traffic
    return traffic


def get_indexer(request: Request) -> MediaIndexer:
    indexer = getattr(request.app.state, "indexer", None)
    if indexer is not None:
        return indexer
    # Unit/in-process API clients do not run the application lifespan.  Build
    # a lightweight indexer so review routes still work without requiring a
    # real TeleBox connection.
    telegram = getattr(request.app.state, "telegram", None)
    if telegram is None:
        override = request.app.dependency_overrides.get(get_telegram)
        telegram = override() if override else None
    if telegram is None:
        raise HTTPException(status_code=503, detail={"code": "INDEXER_UNAVAILABLE"})
    database = getattr(request.app.state, "database", None)
    if database is None:
        database_override = request.app.dependency_overrides.get(get_database)
        database = database_override() if database_override else None
    if database is None:
        raise HTTPException(status_code=503, detail={"code": "DATABASE_UNAVAILABLE"})
    indexer = MediaIndexer(database, telegram)
    request.app.state.indexer = indexer
    return indexer


def get_replication(
    request: Request,
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> DisasterRecoveryManager:
    manager = getattr(request.app.state, "replication", None)
    if manager is None:
        manager = DisasterRecoveryManager(database, telegram)
        request.app.state.replication = manager
    return manager


def _hash_public_key(value: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(value.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _verify_public_key(value: str, encoded: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = bytes.fromhex(_hash_public_key(value, bytes.fromhex(salt_hex)).split("$", 2)[2])
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


async def _public_album_config(database: Database) -> tuple[bool, str, int]:
    enabled = await database.get_setting("public_album_enabled", "0") == "1"
    key_hash = await database.get_setting("public_album_key_hash", "")
    try:
        version = int(await database.get_setting("public_album_key_version", "1"))
    except ValueError:
        version = 1
    return enabled, key_hash, version


async def _registration_config(database: Database) -> tuple[bool, str, int, str, bool]:
    enabled = await database.get_setting("public_registration_enabled", "0") == "1"
    key_hash = await database.get_setting("registration_key_hash", "")
    try:
        version = int(await database.get_setting("registration_key_version", "1"))
    except ValueError:
        version = 1
    fingerprint = await database.get_setting("registration_key_fingerprint", "")
    requires_approval = await database.get_setting("registration_requires_approval", "1") != "0"
    return enabled, key_hash, version, fingerprint, requires_approval


async def _active_telegram_bindings(telegram: TeleBoxClient) -> list[dict[str, Any]] | None:
    getter = getattr(telegram, "bindings", None)
    if getter is None:
        return None
    try:
        items = (await getter()).get("items", [])
    except (AttributeError, TelegramUnavailable):
        return None
    return [item for item in items if isinstance(item, dict)]


async def _sync_auth_user_binding(
    auth: AuthStore,
    telegram: TeleBoxClient,
    user: dict[str, Any] | None,
    *,
    requires_approval: bool,
    bindings: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if not user or not user.get("telegram_user_id") or str(user.get("role")) != "user":
        return user
    if bindings is None:
        bindings = await _active_telegram_bindings(telegram)
        if bindings is None:
            return user
    telegram_user_id = str(user["telegram_user_id"])
    binding = next(
        (
            item
            for item in bindings
            if str(item.get("telegram_user_id")) == telegram_user_id
            and bool(int(item.get("enabled") or 0))
            and not bool(int(item.get("banned") or 0))
            and str(item.get("account_id") or "").strip()
        ),
        None,
    )
    if not binding:
        if str(user.get("binding_sync_status") or "pending") == "pending":
            return user
        return await auth.update_user(int(user["id"]), binding_sync_status="pending")

    update: dict[str, Any] = {
        "account_id": str(binding["account_id"]).strip(),
        "binding_sync_status": "ready",
    }
    # Persist the logical account group so a later failover transparently
    # updates the user's physical route without requiring re-binding.
    group_id = None
    try:
        # AuthStore and SavedStream share the same SQLite path; this lightweight
        # lookup avoids changing the binding API shape.
        async with aiosqlite.connect(auth.path) as db:  # type: ignore[attr-defined]
            cursor = await db.execute("SELECT group_id FROM telegram_account_group_members WHERE account_id=?", (str(binding["account_id"]).strip(),))
            row = await cursor.fetchone()
            group_id = str(row[0]) if row and row[0] else None
    except Exception:
        group_id = None
    if group_id:
        update["account_group_id"] = group_id
    auto_approved = not requires_approval and str(user.get("status")) == "pending"
    if auto_approved:
        update["status"] = "approved"
    synced = await auth.update_user(int(user["id"]), **update)
    if auto_approved:
        await auth.audit(int(user["id"]), "registration_auto_approved")
    return synced


async def _helper_bot_link(telegram: TeleBoxClient, token: str) -> str | None:
    try:
        username = (await telegram.helper_bot_status()).get("username")
    except TelegramUnavailable:
        username = None
    return f"https://t.me/{username}?start={quote(token)}" if username else None


def _safe_user(user: dict[str, Any] | None) -> dict[str, Any] | None:
    if not user:
        return None
    return {
        "id": user.get("id"),
        "username": user.get("username_display") or user.get("username_normalized"),
        "role": user.get("role"),
        "status": user.get("status"),
        "telegram_user_id": user.get("telegram_user_id"),
        "telegram_username": user.get("telegram_username"),
        "display_name": user.get("display_name"),
        "account_id": user.get("account_id"),
        "binding_sync_status": user.get("binding_sync_status"),
        "legacy_claim_required": bool(user.get("legacy_claim_required")),
        "ban_reason": user.get("ban_reason"),
        "created_at": user.get("created_at"),
        "approved_at": user.get("approved_at"),
    }


async def optional_access_principal(
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
    admin_cookie: str | None = Cookie(default=None, alias=ADMIN_COOKIE),
    auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE),
) -> AccessPrincipal | None:
    user = await auth.get_session(auth_cookie)
    if user:
        is_admin = str(user.get("role")) in {"admin", "superadmin"} and str(user.get("status")) == "approved"
        # Prefer a real administrator portal session over the recovery-key
        # cookie.  Both may exist after an administrator signs in again; the
        # portal identity is required to attribute likes and mailbox items.
        if is_admin or not signer.verify(admin_cookie, "admin", "control"):
            return AccessPrincipal(
                is_admin=is_admin,
                user_id=int(user["id"]),
                username=str(user.get("username_display") or user.get("username_normalized") or "") or None,
                role=str(user.get("role") or "user"),
                telegram_user_id=str(user["telegram_user_id"]) if user.get("telegram_user_id") else None,
                account_id=str(user["account_id"]) if user.get("account_id") else None,
                user_status=str(user["status"]),
                binding_sync_status=str(user.get("binding_sync_status") or "pending"),
                public_authenticated=True,
            )
    if signer.verify(admin_cookie, "admin", "control"):
        recovery = await auth.get_or_create_recovery_admin()
        return AccessPrincipal(
            is_admin=True,
            user_id=int(recovery["id"]),
            username=str(recovery.get("username_display") or "Recovery administrator"),
            role="superadmin",
            user_status="approved",
            binding_sync_status="ready",
            public_authenticated=True,
        )
    return None


async def require_admin(
    principal: AccessPrincipal | None = Depends(optional_access_principal),
) -> AccessPrincipal:
    if principal and principal.is_admin:
        return principal
    raise HTTPException(status_code=401, detail={"code": "ADMIN_AUTH_REQUIRED"})


def _is_media_owner(item: dict[str, Any], principal: AccessPrincipal) -> bool:
    if principal.user_id is not None and item.get("owner_user_id") is not None:
        return int(item["owner_user_id"]) == int(principal.user_id)
    return bool(
        item.get("submitter_telegram_user_id")
        and principal.telegram_user_id
        and str(item["submitter_telegram_user_id"]) == str(principal.telegram_user_id)
    )


def _sanction_detail(sanction: dict[str, Any], code: str) -> dict[str, Any]:
    return {
        "code": code,
        "sanction_type": sanction.get("sanction_type"),
        "reason": sanction.get("reason") or "违反平台规则",
        "expires_at": sanction.get("expires_at"),
        "permanent": sanction.get("expires_at") is None,
    }


async def _active_sanction(
    database: Database,
    principal: AccessPrincipal,
    *sanction_types: str,
) -> dict[str, Any] | None:
    # The recovery ADMIN_KEY principal has no database user and therefore no
    # sanction record.  Named administrator/superadministrator accounts are
    # still subject to sanctions created by an authorized superadministrator.
    if principal.user_id is None:
        return None
    return await database.active_user_sanction(int(principal.user_id), sanction_types)


def _validated_expiry(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_SANCTION_EXPIRY"}) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if parsed <= datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail={"code": "INVALID_SANCTION_EXPIRY"})
    return parsed.isoformat()


async def _assert_can_moderate_user(
    principal: AccessPrincipal,
    target: dict[str, Any],
    auth: AuthStore,
) -> None:
    target_id = int(target["id"])
    if principal.user_id is not None and int(principal.user_id) == target_id:
        raise HTTPException(status_code=409, detail={"code": "SELF_SANCTION_FORBIDDEN"})
    target_role = str(target.get("role") or "user")
    if target_role in {"admin", "superadmin"} and principal.role != "superadmin":
        raise HTTPException(status_code=403, detail={"code": "SUPERADMIN_REQUIRED"})
    if target_role == "superadmin" and str(target.get("status")) == "approved" and await auth.superadmin_count() <= 1:
        raise HTTPException(status_code=409, detail={"code": "LAST_SUPERADMIN_REQUIRED"})


async def require_media_access(
    principal: AccessPrincipal | None = Depends(optional_access_principal),
    database: Database = Depends(get_database),
) -> AccessPrincipal:
    public_enabled = await database.get_setting("public_album_enabled", "0") == "1"
    if principal and (principal.is_admin or (
        principal.user_status == "approved"
        and principal.binding_sync_status == "ready"
        and public_enabled
    )):
        return principal
    if principal:
        if principal.user_status != "approved":
            raise HTTPException(status_code=403, detail={"code": f"ACCESS_{principal.user_status.upper()}"})
        if principal.binding_sync_status != "ready":
            raise HTTPException(status_code=403, detail={"code": "BINDING_SYNC_PENDING"})
        raise HTTPException(status_code=403, detail={"code": "PUBLIC_ALBUM_DISABLED"})
    raise HTTPException(status_code=401, detail={"code": "MEDIA_AUTH_REQUIRED"})


require_viewer = require_media_access


async def require_upload_access(
    principal: AccessPrincipal = Depends(require_media_access),
    database: Database = Depends(get_database),
) -> AccessPrincipal:
    sanction = await _active_sanction(database, principal, "login_ban", "upload_mute")
    if sanction:
        code = "LOGIN_BANNED" if sanction.get("sanction_type") == "login_ban" else "UPLOAD_MUTED"
        raise HTTPException(status_code=403, detail=_sanction_detail(sanction, code))
    return principal


async def require_report_access(
    principal: AccessPrincipal = Depends(require_media_access),
    database: Database = Depends(get_database),
) -> AccessPrincipal:
    sanction = await _active_sanction(database, principal, "login_ban", "report_mute")
    if sanction:
        code = "LOGIN_BANNED" if sanction.get("sanction_type") == "login_ban" else "REPORTING_DISABLED"
        raise HTTPException(status_code=403, detail=_sanction_detail(sanction, code))
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
    return principal


async def authorized_account(
    requested_account: str | None,
    principal: AccessPrincipal,
    telegram: TeleBoxClient,
    database: Database | None = None,
) -> str:
    if not principal.is_admin:
        requested = (requested_account or principal.account_id or "").strip()
        if requested in {"", "all", "*"}:
            requested = principal.account_id or ""
        if not requested:
            raise HTTPException(status_code=403, detail={"code": "ACCOUNT_ACCESS_DENIED"})
        if database:
            group = await database.account_group_for_account(requested)
            if group:
                requested = str(group.get("active_account_id") or requested)
        accounts = (await telegram.accounts()).get("items", [])
        if not any(str(item.get("id")) == requested for item in accounts):
            raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
        if not any(str(item.get("id")) == requested and str(item.get("state")) == "authenticated" for item in accounts):
            raise HTTPException(status_code=409, detail={"code": "ACTIVE_ACCOUNT_UNAVAILABLE", "account_id": requested})
        # Cross-account message access is allowed only so the public square can
        # stream approved public media.  indexed_media_for_principal performs
        # the final public/owner authorization and keeps private rows opaque.
        return requested
    if database:
        group = await database.account_group_for_account(requested_account or settings.telebox_default_account)
        if group:
            return str(group.get("active_account_id") or requested_account or settings.telebox_default_account)
    return await telegram.resolve_account(requested_account or settings.telebox_default_account)


async def account_filter(
    requested_account: str | None,
    principal: AccessPrincipal,
    telegram: TeleBoxClient,
    database: Database | None = None,
) -> str | None:
    """Resolve a list filter while allowing public media across accounts."""
    requested = (requested_account or "").strip()
    if not requested or requested in {"all", "*"}:
        return None
    if database:
        group = await database.account_group_for_account(requested)
        if group:
            requested = str(group.get("active_account_id") or requested)
    accounts = (await telegram.accounts()).get("items", [])
    if not any(str(item.get("id")) == requested for item in accounts):
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    if not any(str(item.get("id")) == requested and str(item.get("state")) == "authenticated" for item in accounts):
        raise HTTPException(status_code=409, detail={"code": "ACTIVE_ACCOUNT_UNAVAILABLE", "account_id": requested})
    return requested


async def automatic_upload_account(
    principal: AccessPrincipal,
    telegram: TeleBoxClient,
    database: Database,
) -> tuple[str, str | None]:
    """Choose the physical Saved Messages account without a WebUI selector.

    Regular users keep their logical binding.  Administrators prefer the
    configured/default logical group's active account, then any authenticated
    active group, and finally the first authenticated standalone account.
    """
    payload = await telegram.accounts()
    accounts = list(payload.get("items", []))
    authenticated = {
        str(item.get("id")): item
        for item in accounts
        if str(item.get("state")) == "authenticated" and item.get("id")
    }
    if not principal.is_admin:
        bound_account = str(principal.account_id or "").strip()
        if not bound_account:
            raise HTTPException(status_code=403, detail={"code": "ACCOUNT_ACCESS_DENIED"})
        group = await database.account_group_for_account(bound_account)
        group_id = str(group.get("id")) if group else None
        selected = str(group.get("active_account_id") or bound_account) if group else bound_account
        if selected not in authenticated:
            raise HTTPException(
                status_code=409,
                detail={"code": "ACTIVE_ACCOUNT_UNAVAILABLE", "account_group_id": group_id, "account_id": selected},
            )
        return selected, group_id

    preferred = str(settings.telebox_default_account or "").strip()
    if preferred:
        preferred_group = await database.account_group_for_account(preferred)
        if preferred_group:
            active = str(preferred_group.get("active_account_id") or preferred)
            if active in authenticated:
                return active, str(preferred_group.get("id"))
        if preferred in authenticated:
            group = await database.account_group_for_account(preferred)
            return preferred, str(group.get("id")) if group else None
    for group in await database.list_account_groups():
        active = str(group.get("active_account_id") or "")
        if active in authenticated:
            return active, str(group.get("id"))
    if authenticated:
        selected = next(iter(authenticated))
        group = await database.account_group_for_account(selected)
        return selected, str(group.get("id")) if group else None
    raise HTTPException(status_code=409, detail={"code": "ACTIVE_ACCOUNT_UNAVAILABLE"})


async def indexed_media_for_principal(
    database: Database,
    account_id: str,
    message_id: int,
    principal: AccessPrincipal,
) -> dict:
    item = await database.get_media_index(account_id, message_id, include_provenance=not principal.is_admin)
    if not item:
        raise HTTPException(status_code=409, detail={"code": "MEDIA_INDEX_PENDING"})
    if not principal.is_admin and not (
        item.get("visibility") == "public"
        and item.get("review_status") == "approved"
        and not item.get("hidden")
    ) and not (_is_media_owner(item, principal) and not item.get("hidden")):
        # Do not reveal whether a private message ID exists.
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    return item


async def _admin_traffic_bypass(principal: AccessPrincipal, database: Database) -> bool:
    if not principal.is_admin:
        return False
    configured = await database.get_traffic_settings()
    return bool(int(configured.get("admin_bypass", 0)))


@app.exception_handler(TelegramUnavailable)
async def telegram_unavailable_handler(_: Request, exc: TelegramUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc), "code": "TELEGRAM_UNAVAILABLE"})


@app.exception_handler(MediaNotFound)
async def media_not_found_handler(_: Request, exc: MediaNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc), "code": "MEDIA_NOT_FOUND"})


@app.exception_handler(TrafficLimitExceeded)
async def traffic_limit_handler(_: Request, exc: TrafficLimitExceeded) -> JSONResponse:
    snapshot = exc.snapshot
    return JSONResponse(
        status_code=509,
        content={
            "detail": {
                "code": "TRAFFIC_LIMIT_REACHED",
                "message": "本月媒体流量额度已用尽",
                "used_bytes": int(snapshot.get("used_bytes", 0)),
                "remaining_bytes": int(snapshot.get("remaining_bytes", 0)),
                "monthly_limit_bytes": int(snapshot.get("monthly_limit_bytes", 0)),
            },
            "code": "TRAFFIC_LIMIT_REACHED",
        },
        headers={"Retry-After": "3600"},
    )


@app.exception_handler(UploadQuotaExceeded)
async def upload_quota_handler(_: Request, exc: UploadQuotaExceeded) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    return JSONResponse(
        status_code=429,
        content={
            "detail": {"code": "UPLOAD_QUOTA_REACHED", **detail},
            "code": "UPLOAD_QUOTA_REACHED",
        },
    )


@app.get("/api/status")
async def public_status(
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
    telegram: TeleBoxClient = Depends(get_telegram),
    admin_cookie: str | None = Cookie(default=None, alias=ADMIN_COOKIE),
    principal: AccessPrincipal | None = Depends(optional_access_principal),
) -> dict:
    tg_status = await telegram.status()
    registration_enabled, _, _, _, registration_requires_approval = await _registration_config(database)
    if principal and principal.user_id and not principal.is_admin:
        user = await _sync_auth_user_binding(
            auth,
            telegram,
            await auth.get_user(principal.user_id),
            requires_approval=registration_requires_approval,
        )
        if user:
            principal = replace(
                principal,
                user_status=str(user.get("status") or principal.user_status),
                account_id=str(user["account_id"]) if user.get("account_id") else None,
                binding_sync_status=str(user.get("binding_sync_status") or "pending"),
            )
    admin_authenticated = bool(principal and principal.is_admin) or signer.verify(admin_cookie, "admin", "control")
    public_enabled, public_key_hash, _ = await _public_album_config(database)
    media_authenticated = bool(principal and (principal.is_admin or (
        principal.user_status == "approved"
        and principal.binding_sync_status == "ready"
        and public_enabled
    )))
    media_session_id = None
    if media_authenticated and principal:
        media_session_id = hashlib.sha256(
            f"{settings.admin_key}\0{settings.api_hash}\0{principal.user_id}\0{principal.username}".encode("utf-8")
        ).hexdigest()[:32]
    try:
        helper_bot_username = (await telegram.helper_bot_status()).get("username")
    except TelegramUnavailable:
        helper_bot_username = None
    return {
        "configuration_ok": settings.configuration_ok,
        "telegram_authenticated": tg_status["authenticated"],
        "telegram_state": tg_status["state"],
        "telegram_error": tg_status["error"],
        "access_restricted": True,
        "viewer_authenticated": media_authenticated,
        "admin_authenticated": admin_authenticated,
        "media_authenticated": media_authenticated,
        "access_status": "admin" if admin_authenticated else principal.user_status if principal else "unauthenticated",
        "access_account_id": principal.account_id if principal and not principal.is_admin else None,
        "helper_bot_username": helper_bot_username,
        "public_album_enabled": public_enabled,
        "public_key_configured": bool(public_key_hash),
        "public_authenticated": media_authenticated,
        "personal_features_available": bool(principal and principal.user_id is not None),
        "media_session_id": media_session_id,
        "registration_enabled": registration_enabled,
        "registration_requires_approval": registration_requires_approval,
        "binding_sync_status": principal.binding_sync_status if principal else None,
        "admin_user": {
            "id": principal.user_id,
            "username": principal.username,
            "role": principal.role,
        } if principal and principal.is_admin else None,
    }


@app.post("/api/admin/login")
async def admin_login(
    payload: UserPasswordPayload | KeyPayload,
    response: Response,
    auth: AuthStore = Depends(get_auth),
) -> dict[str, Any]:
    data = payload.model_dump()
    if "key" in data:
        if not settings.admin_key or not constant_time_equal(str(data["key"]), settings.admin_key):
            await asyncio.sleep(0.35)
            raise HTTPException(status_code=401, detail={"code": "INVALID_ADMIN_KEY"})
        response.set_cookie(ADMIN_COOKIE, signer.issue("admin", "control", COOKIE_TTL), max_age=COOKIE_TTL, httponly=True, secure=settings.cookie_secure, samesite="strict", path="/")
        return {"ok": True, "recovery": True}
    try:
        username = validate_username(str(data.get("username", "")))
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "INVALID_USERNAME"})
    user = await auth.get_user_by_username(username)
    if not user or not user.get("password_hash") or not __import__("app.auth", fromlist=["verify_password"]).verify_password(str(data.get("password", "")), user.get("password_hash")):
        await asyncio.sleep(0.25)
        raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS"})
    if str(user.get("role")) not in {"admin", "superadmin"} or str(user.get("status")) != "approved":
        raise HTTPException(status_code=403, detail={"code": "ADMIN_ACCESS_DENIED"})
    token = await auth.create_session(int(user["id"]), None, COOKIE_TTL)
    response.set_cookie(AUTH_COOKIE, token, max_age=COOKIE_TTL, httponly=True, secure=settings.cookie_secure, samesite="strict", path="/")
    return {"ok": True, "user": {"id": user["id"], "username": user.get("username_display"), "role": user.get("role")}}


@app.post("/api/admin/logout")
async def admin_logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(ADMIN_COOKIE, path="/")
    response.delete_cookie(AUTH_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/access/login")
async def viewer_login(
) -> dict[str, bool]:
    raise HTTPException(status_code=410, detail={"code": "VIEWER_PASSWORD_REMOVED"})


@app.post("/api/admin/bootstrap")
async def admin_bootstrap(
    payload: AdminBootstrapPayload,
    response: Response,
    auth: AuthStore = Depends(get_auth),
) -> dict[str, Any]:
    if await auth.admin_count() > 0:
        raise HTTPException(status_code=409, detail={"code": "ADMIN_ALREADY_INITIALIZED"})
    if not settings.admin_key or not constant_time_equal(payload.admin_key, settings.admin_key):
        raise HTTPException(status_code=401, detail={"code": "INVALID_ADMIN_KEY"})
    try:
        user = await auth.create_admin(payload.username, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_ADMIN_BOOTSTRAP", "message": str(exc)}) from exc
    token = await auth.create_session(int(user["id"]), None, COOKIE_TTL)
    response.set_cookie(AUTH_COOKIE, token, max_age=COOKIE_TTL, httponly=True, secure=settings.cookie_secure, samesite="strict", path="/")
    return {"ok": True, "user": {"id": user["id"], "username": user.get("username_display"), "role": user.get("role")}}


@app.post("/api/admin/recovery")
async def admin_recovery(
    payload: KeyPayload,
    response: Response,
) -> dict[str, bool]:
    if not settings.admin_key or not constant_time_equal(payload.key, settings.admin_key):
        raise HTTPException(status_code=401, detail={"code": "INVALID_ADMIN_KEY"})
    response.set_cookie(ADMIN_COOKIE, signer.issue("admin", "control", COOKIE_TTL), max_age=COOKIE_TTL, httponly=True, secure=settings.cookie_secure, samesite="strict", path="/")
    return {"ok": True, "recovery": True}


@app.post("/api/auth/register/start")
async def auth_register_start(
    payload: RegisterPayload,
    request: Request,
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict[str, Any]:
    enabled, key_hash, version, _, _ = await _registration_config(database)
    if not enabled:
        raise HTTPException(status_code=403, detail={"code": "REGISTRATION_DISABLED"})
    if not key_hash or not _verify_public_key(payload.registration_key, key_hash):
        await asyncio.sleep(0.2)
        raise HTTPException(status_code=401, detail={"code": "INVALID_REGISTRATION_KEY"})
    try:
        token, challenge = await auth.register_challenge(
            payload.username,
            payload.password,
            browser_id_hash=hash_browser_id(request.headers.get(BROWSER_ID_HEADER, "")),
            trust_requested=payload.trust_device,
        )
    except ValueError as exc:
        code = "USERNAME_TAKEN" if "already" in str(exc) else "INVALID_REGISTRATION"
        raise HTTPException(status_code=422, detail={"code": code, "message": str(exc)}) from exc
    link = await _helper_bot_link(telegram, token)
    return {**challenge, "registration_key_version": version, "telegram_bot_link": link}


@app.get("/api/auth/register/status")
async def auth_register_status(
    challenge_id: str = Query(min_length=20, max_length=256),
    auth: AuthStore = Depends(get_auth),
) -> dict[str, Any]:
    challenge = await auth.get_challenge(challenge_id, kind="register")
    if not challenge:
        raise HTTPException(status_code=404, detail={"code": "AUTH_CHALLENGE_NOT_FOUND"})
    user = await auth.get_user(int(challenge["user_id"])) if challenge.get("user_id") else None
    return {"challenge_id": challenge_id, "status": "bound" if challenge["status"] == "claimed" else "pending", "user": _safe_user(user)}


@app.post("/api/auth/login")
async def auth_login(
    payload: UserPasswordPayload,
    response: Response,
    request: Request,
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict[str, Any]:
    try:
        username = validate_username(payload.username)
    except ValueError:
        raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS"})
    user = await auth.get_user_by_username(username)
    from .auth import verify_password
    if not user or not user.get("password_hash") or not verify_password(payload.password, user.get("password_hash")):
        await auth.audit(int(user["id"]) if user else None, "login_failed")
        await asyncio.sleep(0.25)
        raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS"})
    login_ban = await database.active_user_sanction(int(user["id"]), ["login_ban"])
    if login_ban:
        await auth.audit(int(user["id"]), "login_blocked_by_sanction")
        raise HTTPException(status_code=403, detail=_sanction_detail(login_ban, "LOGIN_BANNED"))
    user = await _sync_auth_user_binding(
        auth,
        telegram,
        user,
        requires_approval=(await _registration_config(database))[4],
    ) or user
    if user.get("status") in {"disabled", "denied"}:
        raise HTTPException(status_code=403, detail={"code": f"AUTH_{str(user['status']).upper()}"})
    browser_id = request.headers.get(BROWSER_ID_HEADER, "")
    if str(user.get("role")) in {"admin", "superadmin"} or str(user.get("status")) != "approved":
        token = await auth.create_session(int(user["id"]), browser_id, COOKIE_TTL)
        response.set_cookie(AUTH_COOKIE, token, max_age=COOKIE_TTL, httponly=True, secure=settings.cookie_secure, samesite="strict", path="/")
        return {"ok": True, "status": user.get("status"), "user": _safe_user(user), "requires_device": False}
    if await auth.is_trusted_device(int(user["id"]), browser_id):
        token = await auth.create_session(int(user["id"]), browser_id, COOKIE_TTL)
        response.set_cookie(AUTH_COOKIE, token, max_age=COOKIE_TTL, httponly=True, secure=settings.cookie_secure, samesite="strict", path="/")
        return {"ok": True, "status": user.get("status"), "user": _safe_user(user), "requires_device": False}
    challenge_token, challenge = await auth.create_user_challenge("device_verify", int(user["id"]), browser_id_hash=hash_browser_id(browser_id), trust_requested=payload.trust_device)
    return {"ok": True, "status": user.get("status"), "user": _safe_user(user), "requires_device": True, **challenge, "telegram_bot_link": await _helper_bot_link(telegram, challenge_token)}


@app.get("/api/auth/session")
async def auth_session(principal: AccessPrincipal | None = Depends(optional_access_principal)) -> dict[str, Any]:
    return {"authenticated": bool(principal), "user": {
        "id": principal.user_id, "username": principal.username, "role": principal.role,
        "status": principal.user_status, "telegram_user_id": principal.telegram_user_id,
        "account_id": principal.account_id, "binding_sync_status": principal.binding_sync_status,
    } if principal else None}


@app.post("/api/auth/logout")
async def auth_logout(
    response: Response,
    auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE),
    auth: AuthStore = Depends(get_auth),
) -> dict[str, bool]:
    await auth.revoke_session(auth_cookie)
    for cookie in (AUTH_COOKIE, ADMIN_COOKIE, ACCESS_COOKIE, PUBLIC_COOKIE, DEVICE_COOKIE):
        response.delete_cookie(cookie, path="/")
    return {"ok": True}


@app.post("/api/auth/device/verify/start")
async def auth_device_verify_start(
    request: Request,
    auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE),
    auth: AuthStore = Depends(get_auth),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict[str, Any]:
    session = await auth.get_session(auth_cookie)
    if not session:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
    token, challenge = await auth.create_user_challenge("device_verify", int(session["id"]), browser_id_hash=hash_browser_id(request.headers.get(BROWSER_ID_HEADER, "")))
    return {**challenge, "telegram_bot_link": await _helper_bot_link(telegram, token)}


@app.get("/api/auth/device/verify/status")
async def auth_device_verify_status(
    response: Response,
    request: Request,
    challenge_id: str = Query(min_length=20, max_length=256),
    auth: AuthStore = Depends(get_auth),
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    challenge = await auth.get_challenge(challenge_id, kind="device_verify")
    if not challenge:
        raise HTTPException(status_code=404, detail={"code": "AUTH_CHALLENGE_NOT_FOUND"})
    if challenge["status"] == "claimed":
        user = await auth.get_user(int(challenge["user_id"]))
        login_ban = await database.active_user_sanction(int(challenge["user_id"]), ["login_ban"])
        if login_ban:
            raise HTTPException(status_code=403, detail=_sanction_detail(login_ban, "LOGIN_BANNED"))
        browser_id = request.headers.get(BROWSER_ID_HEADER, "")
        token = await auth.create_session(int(challenge["user_id"]), browser_id, COOKIE_TTL, trust_device=bool(challenge.get("trust_requested")))
        response.set_cookie(AUTH_COOKIE, token, max_age=COOKIE_TTL, httponly=True, secure=settings.cookie_secure, samesite="strict", path="/")
        return {"status": "verified", "authenticated": True, "user": _safe_user(user)}
    return {"status": "pending", "authenticated": False}


@app.post("/api/auth/password/reset/start")
async def auth_password_reset_start(
    payload: KeyPayload,
    auth: AuthStore = Depends(get_auth),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict[str, Any]:
    user = await auth.get_user_by_username(payload.key)
    if not user or not user.get("telegram_user_id"):
        return {"ok": True, "message": "If the account exists, Telegram instructions have been created."}
    token, challenge = await auth.create_user_challenge("password_reset", int(user["id"]))
    return {"ok": True, **challenge, "telegram_bot_link": await _helper_bot_link(telegram, token)}


@app.post("/api/auth/password/reset/complete")
async def auth_password_reset_complete(payload: PasswordResetCompletePayload, auth: AuthStore = Depends(get_auth)) -> dict[str, Any]:
    try:
        user = await auth.consume_challenge(payload.challenge_id, "password_reset", new_password=payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_RESET_CHALLENGE", "message": str(exc)}) from exc
    return {"ok": True, "user": _safe_user(user)}


@app.post("/api/auth/legacy-claim/start")
async def auth_legacy_claim_start(
    payload: RegisterPayload,
    auth: AuthStore = Depends(get_auth),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict[str, Any]:
    user = await auth.get_user_by_username(payload.username)
    if user and int(user.get("legacy_claim_required") or 0):
        token, challenge = await auth.register_challenge(payload.username, payload.password, kind="legacy_claim", user_id=int(user["id"]))
        return {**challenge, "telegram_bot_link": await _helper_bot_link(telegram, token)}
    raise HTTPException(status_code=404, detail={"code": "LEGACY_CLAIM_NOT_FOUND"})


@app.get("/api/auth/legacy-claim/status")
async def auth_legacy_claim_status(challenge_id: str = Query(min_length=20, max_length=256), auth: AuthStore = Depends(get_auth)) -> dict[str, Any]:
    challenge = await auth.get_challenge(challenge_id, kind="legacy_claim")
    if not challenge:
        raise HTTPException(status_code=404, detail={"code": "AUTH_CHALLENGE_NOT_FOUND"})
    return {"status": "bound" if challenge["status"] == "claimed" else "pending", "user": _safe_user(await auth.get_user(int(challenge["user_id"]))) if challenge.get("user_id") else None}


@app.post("/api/internal/auth/telegram-challenge/claim")
async def internal_claim_telegram_challenge(
    payload: TelegramChallengeClaimPayload,
    authorization: str | None = Header(default=None),
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict[str, Any]:
    expected = settings.savedstream_internal_token or settings.telebox_api_token
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not expected or not supplied or not constant_time_equal(supplied, expected):
        raise HTTPException(status_code=401, detail={"code": "INTERNAL_AUTH_REQUIRED"})
    existing_user = await auth.get_user_by_telegram(payload.telegram_user_id)
    if existing_user:
        login_ban = await database.active_user_sanction(int(existing_user["id"]), ["login_ban"])
        if login_ban:
            raise HTTPException(status_code=403, detail=_sanction_detail(login_ban, "LOGIN_BANNED"))
    try:
        challenge = await auth.claim_challenge(
            payload.challenge_token,
            payload.telegram_user_id,
            payload.telegram_username,
            payload.display_name,
            chat_type=payload.chat_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "TELEGRAM_CHALLENGE_REJECTED", "message": str(exc)}) from exc
    user = await auth.get_user(int(challenge["user_id"])) if challenge.get("user_id") else None
    user = await _sync_auth_user_binding(
        auth,
        telegram,
        user,
        requires_approval=(await _registration_config(database))[4],
    )
    return {
        "ok": True,
        "challenge_id": challenge.get("id"),
        "kind": challenge.get("kind"),
        "status": challenge.get("status"),
        "user_id": challenge.get("user_id"),
        "user": _safe_user(user),
        "registration_requires_approval": (await _registration_config(database))[4],
    }


@app.get("/api/internal/moderation/users/{telegram_user_id}")
async def internal_user_moderation(
    telegram_user_id: str,
    authorization: str | None = Header(default=None),
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
) -> dict[str, Any]:
    expected = settings.savedstream_internal_token or settings.telebox_api_token
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not expected or not supplied or not constant_time_equal(supplied, expected):
        raise HTTPException(status_code=401, detail={"code": "INTERNAL_AUTH_REQUIRED"})
    user = await auth.get_user_by_telegram(telegram_user_id)
    if not user:
        return {"known": False, "allowed_auth": True, "allowed_upload": True, "sanctions": []}
    sanctions = await database.list_user_sanctions(int(user["id"]), active_only=True)
    legacy_status = str(user.get("status") or "pending")
    if legacy_status in {"disabled", "denied"}:
        sanctions = [
            {
                "sanction_type": "login_ban",
                "reason": user.get("ban_reason") or ("账号已禁用" if legacy_status == "disabled" else "账号访问已拒绝"),
                "expires_at": None,
                "legacy": True,
            },
            *sanctions,
        ]
    blocked_types = {str(item.get("sanction_type")) for item in sanctions}
    return {
        "known": True,
        "user_id": int(user["id"]),
        "role": str(user.get("role") or "user"),
        "personal_quota_bypass": str(user.get("role") or "user") in {"admin", "superadmin"},
        "status": legacy_status,
        "allowed_auth": "login_ban" not in blocked_types,
        "allowed_upload": not bool(blocked_types & {"login_ban", "upload_mute"}),
        "allowed_report": not bool(blocked_types & {"login_ban", "report_mute"}),
        "sanctions": sanctions,
    }


@app.post("/api/internal/moderation/filename")
async def internal_filename_moderation(
    payload: FilenameCheckPayload,
    authorization: str | None = Header(default=None),
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    expected = settings.savedstream_internal_token or settings.telebox_api_token
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not expected or not supplied or not constant_time_equal(supplied, expected):
        raise HTTPException(status_code=401, detail={"code": "INTERNAL_AUTH_REQUIRED"})
    matches = await _filename_sensitive_matches(database, payload.filename)
    return {"allowed": not matches, "matches": matches[:5]}


@app.get("/api/public/status")
async def public_album_status(
    database: Database = Depends(get_database),
    principal: AccessPrincipal | None = Depends(optional_access_principal),
) -> dict[str, bool | int]:
    enabled, key_hash, version = await _public_album_config(database)
    return {
        "enabled": enabled,
        "key_configured": bool(key_hash),
        "authenticated": bool(principal and principal.public_authenticated),
        "key_version": version,
    }


@app.post("/api/public/login")
async def public_album_login(
    payload: KeyPayload,
    response: Response,
    database: Database = Depends(get_database),
) -> dict[str, bool]:
    raise HTTPException(status_code=410, detail={"code": "PUBLIC_KEY_NOW_REGISTRATION_ONLY"})


@app.post("/api/public/logout")
async def public_album_logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(PUBLIC_COOKIE, path="/")
    response.delete_cookie(DEVICE_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/access/telegram")
async def telegram_access_login(
    payload: TelegramAccessPayload,
    response: Response,
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict:
    raise HTTPException(status_code=410, detail={"code": "AUTH_FLOW_REPLACED"})


@app.get("/api/access/telegram/status")
async def telegram_access_status(
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    access_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
) -> dict:
    raise HTTPException(status_code=410, detail={"code": "AUTH_FLOW_REPLACED"})


@app.post("/api/access/telegram/logout")
async def telegram_access_logout(
    response: Response,
    database: Database = Depends(get_database),
    access_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
) -> dict[str, bool]:
    await database.revoke_access_session(access_cookie)
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(VIEWER_COOKIE, path="/")
    response.delete_cookie(PUBLIC_COOKIE, path="/")
    response.delete_cookie(DEVICE_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/access/logout")
async def viewer_logout(
    response: Response,
    database: Database = Depends(get_database),
    access_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
) -> dict[str, bool]:
    await database.revoke_access_session(access_cookie)
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(VIEWER_COOKIE, path="/")
    response.delete_cookie(PUBLIC_COOKIE, path="/")
    response.delete_cookie(DEVICE_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/auth/qr", dependencies=[Depends(require_admin)])
async def start_qr(telegram: TeleBoxClient = Depends(get_telegram)) -> dict:
    return await telegram.start_qr_login()


@app.get("/api/auth/qr/status", dependencies=[Depends(require_admin)])
async def qr_status(telegram: TeleBoxClient = Depends(get_telegram)) -> dict:
    return await telegram.status()


@app.post("/api/auth/password", dependencies=[Depends(require_admin)])
async def auth_password(
    payload: PasswordPayload,
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict:
    return await telegram.submit_password(payload.password)


@app.post("/api/auth/logout", dependencies=[Depends(require_admin)])
async def telegram_logout(
    reset: bool = Query(default=False),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict[str, bool]:
    await telegram.logout(clear_session=reset)
    return {"ok": True}



async def registered_device_public_key(
    fingerprint: str,
    device_cookie: str | None,
    database: Database,
):
    if not signer.verify(device_cookie, "device", fingerprint):
        raise HTTPException(status_code=403, detail={"code": "DEVICE_SESSION_REQUIRED"})
    record = await database.get_device_key(fingerprint)
    if not record or int(record["revoked"]):
        raise HTTPException(status_code=403, detail={"code": "DEVICE_KEY_REQUIRED"})
    try:
        key = load_device_public_key(str(record["public_key_pem"]))
    except DeviceKeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await database.touch_device_key(fingerprint)
    return key


async def read_media_bytes(
    message: dict,
    item: dict,
    cache_key: str,
    offset: int,
    length: int,
    cache: DiskCache,
    telegram: TeleBoxClient,
) -> bytes:
    total = int(item["size"])
    if offset < 0 or offset >= total:
        raise HTTPException(status_code=416, detail="Media offset is outside the file")
    end = min(total, offset + length)
    parts: list[bytes] = []
    position = offset
    while position < end:
        chunk_index = position // TELEGRAM_CHUNK_SIZE
        chunk_start = chunk_index * TELEGRAM_CHUNK_SIZE
        expected_size = min(TELEGRAM_CHUNK_SIZE, total - chunk_start)
        chunk = await cache.get_chunk(
            cache_key,
            chunk_index,
            expected_size,
            lambda start=chunk_start: telegram.download_chunk(message, start, total),
        )
        local_start = position - chunk_start
        local_end = min(len(chunk), end - chunk_start)
        parts.append(chunk[local_start:local_end])
        position += local_end - local_start
    return b"".join(parts)


@app.get("/api/security/device-key", dependencies=[Depends(require_media_access)])
async def device_key_status(
    x_savedstream_device_key: str | None = Header(default=None),
    device_cookie: str | None = Cookie(default=None, alias=DEVICE_COOKIE),
    database: Database = Depends(get_database),
) -> dict:
    if not x_savedstream_device_key or not signer.verify(device_cookie, "device", x_savedstream_device_key):
        return {"registered": False}
    record = await database.get_device_key(x_savedstream_device_key)
    return {
        "registered": bool(record and not int(record["revoked"])),
        "fingerprint": x_savedstream_device_key,
    }


@app.post("/api/security/device-key", dependencies=[Depends(require_media_access)])
async def register_device_key(
    payload: DeviceKeyPayload,
    response: Response,
    database: Database = Depends(get_database),
) -> dict:
    try:
        parsed = parse_device_public_key(payload.device_public_key)
    except DeviceKeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    registered = await database.register_device_key(parsed.fingerprint, parsed.public_key_pem)
    if not registered:
        raise HTTPException(status_code=403, detail={"code": "DEVICE_KEY_REVOKED"})
    response.set_cookie(
        DEVICE_COOKIE,
        signer.issue("device", parsed.fingerprint, COOKIE_TTL),
        max_age=COOKIE_TTL if payload.persistence == "persistent" else None,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return {"registered": True, "fingerprint": parsed.fingerprint}


@app.delete("/api/security/device-key", dependencies=[Depends(require_media_access)])
async def revoke_device_key(
    response: Response,
    x_savedstream_device_key: str = Header(),
    device_cookie: str | None = Cookie(default=None, alias=DEVICE_COOKIE),
    database: Database = Depends(get_database),
) -> dict[str, bool]:
    if not signer.verify(device_cookie, "device", x_savedstream_device_key):
        raise HTTPException(status_code=403, detail={"code": "DEVICE_SESSION_REQUIRED"})
    await database.revoke_device_key(x_savedstream_device_key)
    response.delete_cookie(DEVICE_COOKIE, path="/")
    return {"ok": True}

@app.get("/api/media", dependencies=[Depends(require_viewer)])
async def list_media(
    limit: int = Query(default=36, ge=1, le=72),
    cursor: str | None = Query(default=None),
    order: str = Query(default="newest", pattern="^(newest|oldest)$"),
    sort_by: str | None = Query(default=None, alias="sort", pattern="^(title|kind|size|date)$"),
    direction: str | None = Query(default=None, pattern="^(asc|desc)$"),
    kind: str = Query(default="all", pattern="^(all|video|image|audio|file)$"),
    q: str = Query(default="", max_length=100),
    account: str | None = Query(default=None, min_length=1, max_length=40),
    scope: str = Query(default="public", pattern="^(public|private|hidden|all)$"),
    view: str = Query(default="private", pattern="^(private|square|my_public|liked)$"),
    folder: int | None = Query(default=None, alias="folder_id", ge=0),
    date_from: str | None = Query(default=None, alias="from", max_length=10),
    date_to: str | None = Query(default=None, alias="to", max_length=10),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    principal: AccessPrincipal = Depends(require_media_access),
) -> dict:
    account_id = await account_filter(account, principal, telegram, database)
    visibility = scope if principal.is_admin else "all"
    # Administrators keep the existing all-media management view under the
    # default "private" tab, but can also use the public square and their own
    # uploader/like collections from the same sidebar as regular users.
    collection = view if (not principal.is_admin or view != "private") else None
    items, next_cursor, has_more = await database.list_media_index(
        account_id=account_id,
        limit=limit,
        cursor=cursor,
        order=order,
        kind=kind,
        query=q.strip(),
        visibility=visibility,
        date_from=date_from,
        date_to=date_to,
        owner_telegram_user_id=principal.telegram_user_id if collection is not None else None,
        owner_user_id=principal.user_id if collection is not None else None,
        collection=collection,
        viewer_user_id=principal.user_id,
        include_provenance=bool(principal.is_admin or view == "my_public"),
        folder_id=folder,
        sort_by=sort_by,
        sort_direction=direction,
    )
    for item in items:
        item_account = str(item["account_id"])
        item["thumbnail_url"] = f"/api/media/{item['id']}/thumbnail?account={quote(item_account)}&size={item['size']}&v=2" if item["has_thumbnail"] else None
        item["stream_url"] = f"/api/media/{item['id']}/stream?account={quote(item_account)}"
    return {
        "items": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "scope": visibility,
        "view": collection,
        "index": await database.get_sync_state(account_id) if account_id else None,
    }


@app.get("/api/media/timeline", dependencies=[Depends(require_viewer)])
async def media_timeline(
    account: str | None = Query(default=None, min_length=1, max_length=40),
    kind: str = Query(default="all", pattern="^(all|video|image|audio|file)$"),
    q: str = Query(default="", max_length=100),
    scope: str = Query(default="public", pattern="^(public|private|hidden|all)$"),
    view: str = Query(default="private", pattern="^(private|square|my_public|liked)$"),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    principal: AccessPrincipal = Depends(require_media_access),
) -> dict:
    account_id = await account_filter(account, principal, telegram, database)
    visibility = scope if principal.is_admin else "all"
    collection = view if (not principal.is_admin or view != "private") else None
    return {
        "account_id": account_id,
        "scope": visibility,
        "view": collection,
        "years": await database.list_timeline(
            account_id=account_id,
            visibility=visibility,
            kind=kind,
            query=q.strip(),
            owner_telegram_user_id=principal.telegram_user_id if collection is not None else None,
            owner_user_id=principal.user_id if collection is not None else None,
            collection=collection,
            viewer_user_id=principal.user_id,
        ),
        "index": await database.get_sync_state(account_id) if account_id else None,
    }


@app.get("/api/accounts", dependencies=[Depends(require_viewer)])
async def public_accounts(
    telegram: TeleBoxClient = Depends(get_telegram),
    database: Database = Depends(get_database),
    principal: AccessPrincipal = Depends(require_media_access),
) -> dict:
    payload = await telegram.accounts()
    items = payload.get("items", [])
    groups = await database.list_account_groups()
    active_ids = {str(group.get("active_account_id")) for group in groups}
    if active_ids:
        items = [item for item in items if str(item.get("id")) in active_ids]
    configured_default = settings.telebox_default_account
    selected_default = configured_default if any(item.get("id") == configured_default for item in items) else next((item["id"] for item in items if item.get("state") == "authenticated"), items[0]["id"] if items else configured_default)
    return {
        "items": [
            {"id": item["id"], "label": item.get("label", item["id"]), "state": item.get("state", "unknown")}
            for item in items
        ],
        "default_account": selected_default,
    }


@app.put("/api/media/{message_id}/like")
async def like_media(
    message_id: int,
    account: str | None = Query(default=None, min_length=1, max_length=40),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    principal: AccessPrincipal = Depends(require_media_access),
) -> dict[str, Any]:
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
    account_id = await authorized_account(account, principal, telegram, database)
    item = await database.get_media_index(account_id, message_id, include_provenance=True)
    if not item or item.get("deleted") or item.get("hidden") or item.get("visibility") != "public" or item.get("review_status") != "approved":
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    if _is_media_owner(item, principal):
        raise HTTPException(status_code=409, detail={"code": "SELF_LIKE_FORBIDDEN"})
    return await database.set_media_like(int(principal.user_id), account_id, message_id, True)


@app.delete("/api/media/{message_id}/like")
async def unlike_media(
    message_id: int,
    account: str | None = Query(default=None, min_length=1, max_length=40),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    principal: AccessPrincipal = Depends(require_media_access),
) -> dict[str, Any]:
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
    account_id = await authorized_account(account, principal, telegram, database)
    return await database.set_media_like(int(principal.user_id), account_id, message_id, False)


@app.post("/api/media/{message_id}/reports", status_code=201)
async def report_media(
    message_id: int,
    payload: MediaReportPayload,
    account: str | None = Query(default=None, min_length=1, max_length=40),
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
    telegram: TeleBoxClient = Depends(get_telegram),
    principal: AccessPrincipal = Depends(require_report_access),
) -> dict[str, Any]:
    account_id = await authorized_account(account, principal, telegram, database)
    item = await database.get_media_index(account_id, message_id, include_provenance=True)
    if not item or item.get("deleted") or item.get("hidden") or item.get("visibility") != "public" or item.get("review_status") != "approved":
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    if _is_media_owner(item, principal):
        raise HTTPException(status_code=409, detail={"code": "SELF_REPORT_FORBIDDEN"})
    owner_user_id = item.get("owner_user_id")
    if owner_user_id is None and item.get("submitter_telegram_user_id"):
        owner = await auth.get_user_by_telegram(str(item["submitter_telegram_user_id"]))
        owner_user_id = owner.get("id") if owner else None
    try:
        report = await database.create_media_report(
            reporter_user_id=int(principal.user_id),
            account_id=account_id,
            message_id=message_id,
            owner_user_id=int(owner_user_id) if owner_user_id is not None else None,
            reason_code=payload.reason_code,
            details=payload.details,
            media_title=str(item.get("title") or item.get("filename") or message_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "REPORT_ALREADY_OPEN"}) from exc
    return {"ok": True, "report_id": report["id"], "status": report["status"]}


@app.get("/api/media/{message_id}/thumbnail", dependencies=[Depends(require_viewer)])
async def media_thumbnail(
    message_id: int,
    account: str | None = Query(default=None, min_length=1, max_length=40),
    size: int | None = Query(default=None, ge=1),
    cache: DiskCache = Depends(get_cache),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    traffic: TrafficController = Depends(get_traffic),
    principal: AccessPrincipal = Depends(require_media_access),
) -> Response:
    account = await authorized_account(account, principal, telegram, database)
    indexed = await indexed_media_for_principal(database, account, message_id, principal)
    if size is not None:
        cache_key = telegram.media_cache_key(
            {"account_id": account, "id": message_id}, {"size": int(indexed["size"])}
        )
        cached = await cache.get_cached_thumbnail(cache_key)
        if cached is not None:
            return Response(
                content=cached,
                media_type=guess_image_content_type(cached),
                headers={"Cache-Control": "private, max-age=604800, immutable"},
            )
    message, item = await telegram.get_media_message(account, message_id)
    if not item["has_thumbnail"]:
        raise MediaNotFound("The media has no thumbnail")
    cache_key = telegram.media_cache_key(message, item)
    data = await cache.get_thumbnail(
        cache_key, lambda: telegram.download_thumbnail(message)
    )
    async with traffic.request("request", "out"):
        await traffic.consume(
            "out",
            len(data),
            bypass_limit=await _admin_traffic_bypass(principal, database),
        )
    return Response(
        content=data,
        media_type=guess_image_content_type(data),
        headers={"Cache-Control": "private, max-age=604800, immutable"},
    )



@app.get("/api/media/{message_id}/encrypted-thumbnail", dependencies=[Depends(require_media_access)])
async def encrypted_thumbnail(
    message_id: int,
    account: str | None = Query(default=None, min_length=1, max_length=40),
    device: str | None = Query(default=None, max_length=128),
    x_savedstream_device_key: str = Header(),
    device_cookie: str | None = Cookie(default=None, alias=DEVICE_COOKIE),
    cache: DiskCache = Depends(get_cache),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    traffic: TrafficController = Depends(get_traffic),
    principal: AccessPrincipal = Depends(require_media_access),
) -> Response:
    account = await authorized_account(account, principal, telegram, database)
    indexed = await indexed_media_for_principal(database, account, message_id, principal)
    # The device query parameter is a browser-cache key.  When present it
    # must match the authenticated fingerprint so a rotated device key cannot
    # be served a stale encrypted thumbnail from the browser cache.
    if device is not None and device != x_savedstream_device_key:
        raise HTTPException(status_code=403, detail={"code": "DEVICE_SESSION_REQUIRED"})
    device_key = await registered_device_public_key(x_savedstream_device_key, device_cookie, database)
    message, item = await telegram.get_media_message(account, message_id)
    if not item["has_thumbnail"]:
        raise MediaNotFound("The media has no thumbnail")
    cache_key = telegram.media_cache_key(message, item)
    data = await cache.get_thumbnail(cache_key, lambda: telegram.download_thumbnail(message))
    aad = f"thumbnail:{account}:{message_id}:{item['size']}".encode("utf-8")
    encrypted, crypto_headers = encrypt_for_device(data, device_key, aad)
    async with traffic.request("request", "out"):
        await traffic.consume(
            "out",
            len(encrypted),
            bypass_limit=await _admin_traffic_bypass(principal, database),
        )
    # With the device cache key present the ciphertext is immutable for the
    # browser: the wrapped key, nonce and AAD travel in the response headers,
    # so a cached response can be decrypted as-is on repeat views.  Requests
    # without the key (older clients) stay uncached.
    cache_control = "private, max-age=604800, immutable" if device is not None else "private, no-store"
    headers = {**crypto_headers, "X-SavedStream-Mime": guess_image_content_type(data), "Cache-Control": cache_control, "Content-Length": str(len(encrypted))}
    return Response(content=encrypted, media_type="application/octet-stream", headers=headers)


@app.get("/api/media/{message_id}/encrypted-chunk", dependencies=[Depends(require_media_access)])
async def encrypted_chunk(
    message_id: int,
    offset: int = Query(default=0, ge=0),
    length: int = Query(default=TELEGRAM_CHUNK_SIZE, ge=1, le=TELEGRAM_CHUNK_SIZE),
    account: str | None = Query(default=None, min_length=1, max_length=40),
    x_savedstream_device_key: str = Header(),
    device_cookie: str | None = Cookie(default=None, alias=DEVICE_COOKIE),
    cache: DiskCache = Depends(get_cache),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    traffic: TrafficController = Depends(get_traffic),
    principal: AccessPrincipal = Depends(require_media_access),
) -> Response:
    account = await authorized_account(account, principal, telegram, database)
    indexed = await indexed_media_for_principal(database, account, message_id, principal)
    device_key = await registered_device_public_key(x_savedstream_device_key, device_cookie, database)
    message, item = await telegram.get_media_message(account, message_id)
    total = int(item["size"])
    if offset >= total:
        raise HTTPException(status_code=416, detail="Media offset is outside the file")
    data = await read_media_bytes(
        message,
        item,
        telegram.media_cache_key(message, item),
        offset,
        min(length, total - offset),
        cache,
        telegram,
    )
    actual_length = len(data)
    aad = f"chunk:{account}:{message_id}:{offset}:{actual_length}:{total}".encode("utf-8")
    encrypted, crypto_headers = encrypt_for_device(data, device_key, aad)
    async with traffic.request("request", "out"):
        await traffic.consume(
            "out",
            len(encrypted),
            bypass_limit=await _admin_traffic_bypass(principal, database),
        )
    headers = {
        **crypto_headers,
        "Cache-Control": "private, no-store",
        "Content-Length": str(len(encrypted)),
        "X-SavedStream-Offset": str(offset),
        "X-SavedStream-Total-Length": str(total),
        "X-SavedStream-Mime": item["mime_type"],
    }
    return Response(content=encrypted, media_type="application/octet-stream", headers=headers)

@app.get("/api/media/{message_id}/stream", dependencies=[Depends(require_viewer)])
async def media_stream(
    message_id: int,
    request: Request,
    download: bool = Query(default=False),
    account: str | None = Query(default=None, min_length=1, max_length=40),
    cache: DiskCache = Depends(get_cache),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    traffic: TrafficController = Depends(get_traffic),
    principal: AccessPrincipal = Depends(require_media_access),
) -> StreamingResponse:
    account = await authorized_account(account, principal, telegram, database)
    indexed = await indexed_media_for_principal(database, account, message_id, principal)
    message, item = await telegram.get_media_message(account, message_id)
    total = int(item["size"])
    cache_key = telegram.media_cache_key(message, item)
    range_header = request.headers.get("range")
    try:
        byte_range = parse_range_header(range_header, total)
    except InvalidRange as exc:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail=str(exc),
            headers={"Content-Range": f"bytes */{total}"},
        ) from exc
    bypass_limit = await _admin_traffic_bypass(principal, database)
    await traffic.ensure_available(byte_range.length, bypass_limit=bypass_limit)

    async def body() -> AsyncIterator[bytes]:
        await traffic.start_request("stream", "out")
        try:
            position = byte_range.start
            while position <= byte_range.end:
                chunk_index = position // TELEGRAM_CHUNK_SIZE
                chunk_start = chunk_index * TELEGRAM_CHUNK_SIZE
                expected_chunk_size = min(TELEGRAM_CHUNK_SIZE, total - chunk_start)
                chunk = await cache.get_chunk(
                    cache_key,
                    chunk_index,
                    expected_chunk_size,
                    lambda start=chunk_start: telegram.download_chunk(message, start, total),
                )
                local_start = position - chunk_start
                local_end = min(len(chunk), byte_range.end - chunk_start + 1)
                data = chunk[local_start:local_end]
                if len(data) != local_end - local_start:
                    raise TelegramUnavailable("Cached media chunk is incomplete")
                try:
                    await traffic.consume("out", len(data), bypass_limit=bypass_limit)
                except TrafficLimitExceeded:
                    # The complete range was checked before headers were sent.
                    # A competing stream can consume the remaining quota in the
                    # meantime; stop this body rather than sending bytes beyond
                    # the hard cap.
                    break
                yield data
                position += len(data)
        finally:
            await traffic.finish_request("stream")

    disposition = "attachment" if download else "inline"
    encoded_filename = quote(item["filename"], safe="")
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(byte_range.length),
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_filename}",
        "Cache-Control": "private, no-store",
    }
    status_code = 206 if range_header else 200
    if range_header:
        headers["Content-Range"] = byte_range.content_range
    return StreamingResponse(
        body(),
        status_code=status_code,
        media_type=item["mime_type"],
        headers=headers,
    )


@app.get("/api/admin/settings", dependencies=[Depends(require_admin)])
async def admin_settings(
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
    cache: DiskCache = Depends(get_cache),
    telegram: TeleBoxClient = Depends(get_telegram),
    traffic: TrafficController = Depends(get_traffic),
    replication: DisasterRecoveryManager = Depends(get_replication),
) -> dict:
    await replication.ensure_groups()
    cache_limit = await database.get_cache_limit()
    cache_stats = await cache.stats()
    viewer_hash = await database.get_setting("viewer_key_hash", "")
    public_enabled, public_key_hash, public_key_version = await _public_album_config(database)
    registration_enabled, registration_hash, registration_version, registration_fingerprint, registration_requires_approval = await _registration_config(database)
    try:
        helper_rate_limit = await telegram.helper_bot_rate_limit()
    except (AttributeError, TelegramUnavailable):
        helper_rate_limit = default_helper_rate_limit()
    account_rows = (await telegram.accounts()).get("items", [])
    account_meta: dict[str, dict[str, Any]] = {}
    for group in await database.list_account_groups():
        for member in group.get("members", []):
            account_meta[str(member.get("account_id"))] = {
                "account_group_id": group.get("id"),
                "account_role": member.get("role"),
                "account_priority": member.get("priority"),
                "account_enabled": bool(member.get("enabled")),
                "replication_status": member.get("sync_status"),
                "replication_processed_files": member.get("processed_files"),
                "replication_processed_bytes": member.get("processed_bytes"),
                "replication_total_files": member.get("total_files"),
                "replication_total_bytes": member.get("total_bytes"),
                "replication_error": member.get("last_error"),
                "active": str(group.get("active_account_id")) == str(member.get("account_id")),
            }
    account_rows = [{**item, **account_meta.get(str(item.get("id")), {})} for item in account_rows]
    return {
        "cache_max_gb": round(cache_limit / (1024**3), 2),
        "cache_bytes": cache_stats["bytes"],
        "cache_files": cache_stats["files"],
        "access_restricted": await database.access_restricted(),
        "viewer_key_configured": bool(viewer_hash),
        "public_album_enabled": public_enabled,
        "public_key_configured": bool(public_key_hash),
        "public_key_version": public_key_version,
        "registration_enabled": registration_enabled,
        "registration_key_configured": bool(registration_hash),
        "registration_key_version": registration_version,
        "registration_key_fingerprint": registration_fingerprint,
        "registration_requires_approval": registration_requires_approval,
        "telegram": await telegram.status(),
        "accounts": account_rows,
        "account_groups": await database.list_account_groups(),
        "helper_bot": await telegram.helper_bot_status(),
        "bindings": (await telegram.bindings()).get("items", []),
        "ingest_jobs": (await telegram.jobs()).get("items", []),
        "access_users": await database.list_media_users(),
        "auth_users": [_safe_user(user) | {"password_hash": None} for user in await auth.list_users()],
        "media_sync": await database.list_sync_states(),
        "upload_jobs": [_public_upload_job(item) for item in await database.list_upload_jobs()],
        "traffic": await _traffic_summary(database, traffic),
        "helper_rate_limit": helper_rate_limit,
        "filename_sensitive": {
            "items": await database.list_filename_sensitive_lists(include_disabled=True),
            "settings": {
                "max_attempts_10m": await _integer_setting(database, "filename_rename_max_attempts_10m", 10),
                "cooldown_seconds": await _integer_setting(database, "filename_rename_cooldown_seconds", 30),
            },
        },
        "bind_invites": await _bind_invite_settings(database),
    }


def default_helper_rate_limit() -> dict[str, int]:
    return {
        "per_user_files_24h": DEFAULT_HELPER_PER_USER_FILES_24H,
        "per_user_bytes_24h": DEFAULT_HELPER_PER_USER_BYTES_24H,
        "per_user_concurrent": DEFAULT_HELPER_PER_USER_CONCURRENT,
        "max_file_bytes": DEFAULT_HELPER_MAX_FILE_BYTES,
        "global_files_per_minute": DEFAULT_HELPER_GLOBAL_FILES_PER_MINUTE,
        "max_album_items": DEFAULT_HELPER_MAX_ALBUM_ITEMS,
        "max_album_bytes": DEFAULT_HELPER_MAX_ALBUM_BYTES,
    }


@app.get("/api/admin/public-album", dependencies=[Depends(require_admin)])
async def admin_public_album(database: Database = Depends(get_database)) -> dict[str, Any]:
    enabled, key_hash, version = await _public_album_config(database)
    registration_enabled, registration_hash, registration_version, fingerprint, registration_requires_approval = await _registration_config(database)
    return {
        "enabled": enabled,
        "key_configured": bool(key_hash),
        "key_version": version,
        "registration_enabled": registration_enabled,
        "registration_key_configured": bool(registration_hash),
        "registration_key_version": registration_version,
        "registration_key_fingerprint": fingerprint,
        "registration_requires_approval": registration_requires_approval,
    }


@app.put("/api/admin/public-album", dependencies=[Depends(require_admin)])
async def update_public_album(
    payload: PublicAlbumSettingsPayload,
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict[str, Any]:
    enabled, album_hash, album_version = await _public_album_config(database)
    registration_enabled, registration_hash, registration_version, fingerprint, registration_requires_approval = await _registration_config(database)
    if payload.enabled is not None:
        if payload.enabled and not album_hash:
            raise HTTPException(status_code=409, detail={"code": "PUBLIC_KEY_NOT_CONFIGURED"})
        enabled = payload.enabled
        await database.set_setting("public_album_enabled", "1" if enabled else "0")
    if payload.registration_key is not None:
        await database.set_setting("registration_key_hash", _hash_public_key(payload.registration_key))
        registration_version += 1
        fingerprint = hashlib.sha256(payload.registration_key.encode("utf-8")).hexdigest()[:16]
        await database.set_setting("registration_key_version", str(registration_version))
        await database.set_setting("registration_key_fingerprint", fingerprint)
        await database.set_setting("public_registration_enabled", "0")
        registration_enabled = False
    if payload.registration_enabled is not None:
        if payload.registration_enabled and not registration_hash and payload.registration_key is None:
            raise HTTPException(status_code=409, detail={"code": "REGISTRATION_KEY_NOT_CONFIGURED"})
        registration_enabled = payload.registration_enabled
        await database.set_setting("public_registration_enabled", "1" if registration_enabled else "0")
    if payload.registration_requires_approval is not None:
        registration_requires_approval = payload.registration_requires_approval
        await database.set_setting(
            "registration_requires_approval",
            "1" if registration_requires_approval else "0",
        )
        if not registration_requires_approval:
            bindings = await _active_telegram_bindings(telegram)
            if bindings is not None:
                for user in await auth.list_users():
                    if str(user.get("status")) == "pending":
                        await _sync_auth_user_binding(
                            auth,
                            telegram,
                            user,
                            requires_approval=False,
                            bindings=bindings,
                        )
    return {
        "enabled": enabled,
        "key_configured": bool(album_hash),
        "key_version": album_version,
        "registration_enabled": registration_enabled,
        "registration_key_configured": bool(registration_hash or payload.registration_key),
        "registration_key_version": registration_version,
        "registration_key_fingerprint": fingerprint,
        "registration_requires_approval": registration_requires_approval,
    }


@app.post("/api/admin/public-album/key", dependencies=[Depends(require_admin)])
async def generate_public_album_key(database: Database = Depends(get_database)) -> dict[str, str | int | bool]:
    raw_key = secrets.token_urlsafe(32)
    _, _, current_version = await _public_album_config(database)
    next_version = current_version + 1
    await database.set_setting("public_album_key_hash", _hash_public_key(raw_key))
    await database.set_setting("public_album_key_version", str(next_version))
    return {
        "key": raw_key,
        "enabled": await database.get_setting("public_album_enabled", "0") == "1",
        "key_configured": True,
        "key_version": next_version,
    }


@app.post("/api/admin/public-album/registration-key", dependencies=[Depends(require_admin)])
async def update_registration_key(
    payload: RegistrationKeyPayload,
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    if payload.key is not None:
        raw_key = payload.key.strip()
        if not raw_key:
            raise HTTPException(status_code=422, detail={"code": "REGISTRATION_KEY_REQUIRED"})
        generated = False
    elif payload.generate:
        raw_key = secrets.token_urlsafe(32)
        generated = True
    else:
        raise HTTPException(status_code=422, detail={"code": "REGISTRATION_KEY_REQUIRED"})
    _, _, current_version, _, _ = await _registration_config(database)
    version = current_version + 1
    fingerprint = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
    await database.set_setting("registration_key_hash", _hash_public_key(raw_key))
    await database.set_setting("registration_key_version", str(version))
    await database.set_setting("registration_key_fingerprint", fingerprint)
    await database.set_setting("public_registration_enabled", "0")
    return {
        "key": raw_key,
        "key_version": version,
        "fingerprint": fingerprint,
        "enabled": False,
        "generated": generated,
    }


@app.get("/api/admin/filename-sensitive", dependencies=[Depends(require_admin)])
@app.get("/api/admin/sensitive-words", dependencies=[Depends(require_admin)])
@app.get("/api/admin/filename-sensitive-lists", dependencies=[Depends(require_admin)])
async def admin_filename_sensitive_lists(
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    try:
        max_attempts = int(await database.get_setting("filename_rename_max_attempts_10m", "10"))
    except ValueError:
        max_attempts = 10
    try:
        cooldown = int(await database.get_setting("filename_rename_cooldown_seconds", "30"))
    except ValueError:
        cooldown = 30
    return {
        "items": await database.list_filename_sensitive_lists(include_disabled=True),
        "settings": {
            "max_attempts_10m": max_attempts,
            "cooldown_seconds": cooldown,
        },
    }


@app.post("/api/admin/filename-sensitive", dependencies=[Depends(require_admin)])
@app.post("/api/admin/sensitive-words", dependencies=[Depends(require_admin)])
@app.post("/api/admin/filename-sensitive-lists", dependencies=[Depends(require_admin)])
async def upload_filename_sensitive_lists(
    files: list[UploadFile] = File(...),
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=422, detail={"code": "SENSITIVE_WORD_FILE_REQUIRED"})
    results: list[dict[str, Any]] = []
    try:
        for upload in files[:50]:
            name = Path(upload.filename or "sensitive-words.txt").name
            if not name.lower().endswith(".txt"):
                raise HTTPException(status_code=422, detail={"code": "SENSITIVE_WORD_FILE_TYPE", "message": "敏感词库必须是 txt 文件"})
            content = await upload.read()
            if len(content) > 10 * 1024 * 1024:
                raise HTTPException(status_code=413, detail={"code": "SENSITIVE_WORD_FILE_TOO_LARGE"})
            if not content:
                raise HTTPException(status_code=422, detail={"code": "SENSITIVE_WORD_FILE_EMPTY", "filename": name})
            try:
                results.append(await database.add_filename_sensitive_list(name, content))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail={"code": "SENSITIVE_WORD_LIMIT", "message": str(exc)}) from exc
    finally:
        for upload in files:
            await upload.close()
    return {"items": results}


@app.delete("/api/admin/filename-sensitive/{list_id}", dependencies=[Depends(require_admin)])
@app.delete("/api/admin/sensitive-words/{list_id}", dependencies=[Depends(require_admin)])
@app.delete("/api/admin/filename-sensitive-lists/{list_id}", dependencies=[Depends(require_admin)])
async def delete_filename_sensitive_list(
    list_id: str,
    database: Database = Depends(get_database),
) -> dict[str, bool]:
    if not await database.delete_filename_sensitive_list(list_id):
        raise HTTPException(status_code=404, detail={"code": "SENSITIVE_WORD_LIST_NOT_FOUND"})
    return {"ok": True}


@app.put("/api/admin/filename-sensitive/settings", dependencies=[Depends(require_admin)])
@app.put("/api/admin/sensitive-words/settings", dependencies=[Depends(require_admin)])
async def update_filename_sensitive_settings(
    payload: FilenameSensitiveSettingsPayload,
    database: Database = Depends(get_database),
) -> dict[str, int]:
    await database.set_setting("filename_rename_max_attempts_10m", str(payload.max_attempts_10m))
    await database.set_setting("filename_rename_cooldown_seconds", str(payload.cooldown_seconds))
    return payload.model_dump()


@app.get("/api/admin/bind-invites", dependencies=[Depends(require_admin)])
@app.get("/api/admin/bind-invite-settings", dependencies=[Depends(require_admin)])
async def admin_bind_invite_settings(database: Database = Depends(get_database)) -> dict[str, Any]:
    return await _bind_invite_settings(database)


@app.put("/api/admin/bind-invites", dependencies=[Depends(require_admin)])
@app.put("/api/admin/bind-invite-settings", dependencies=[Depends(require_admin)])
async def update_bind_invite_settings(
    payload: BindInviteSettingsPayload,
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict[str, Any]:
    updater = getattr(telegram, "set_bind_invite_settings", None)
    if updater is not None:
        try:
            await updater(payload.model_dump())
        except TelegramUnavailable as exc:
            raise HTTPException(status_code=503, detail={"code": "BIND_INVITE_SETTINGS_UNAVAILABLE", "message": str(exc)}) from exc
    await database.set_setting("bind_invites_enabled", "1" if payload.enabled else "0")
    await database.set_setting("bind_invites_global_joins_24h", str(payload.global_joins_24h))
    await database.set_setting("bind_invites_per_user_generation_24h", str(payload.per_user_generation_24h))
    return await _bind_invite_settings(database)


@app.post("/api/bind/invite", status_code=201)
async def create_user_bind_invite(
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    principal: AccessPrincipal = Depends(require_media_access),
) -> dict[str, Any]:
    if principal.is_admin or not principal.telegram_user_id:
        raise HTTPException(status_code=403, detail={"code": "TELEGRAM_IDENTITY_REQUIRED"})
    config = await _bind_invite_settings(database)
    if not config["enabled"]:
        raise HTTPException(status_code=403, detail={"code": "BIND_INVITES_DISABLED"})
    account_id, _ = await automatic_upload_account(principal, telegram, database)
    creator = getattr(telegram, "create_user_invite", None)
    if creator is None:
        raise HTTPException(status_code=503, detail={"code": "BIND_INVITE_UNAVAILABLE"})
    try:
        return await creator(account_id, principal.telegram_user_id)
    except UploadQuotaExceeded as exc:
        raise HTTPException(status_code=429, detail={"code": "BIND_INVITE_RATE_LIMITED", "message": str(exc)}) from exc
    except TelegramUnavailable as exc:
        message = str(exc)
        code = "BIND_INVITE_RATE_LIMITED" if "limit" in message.lower() or "quota" in message.lower() else "BIND_INVITE_UNAVAILABLE"
        raise HTTPException(status_code=429 if code.endswith("LIMITED") else 503, detail={"code": code, "message": message}) from exc


@app.get("/api/admin/reports", dependencies=[Depends(require_admin)])
async def admin_reports(
    status_filter: str = Query(default="open", alias="status", pattern="^(open|actionable|processing|resolved|ignored|failed|all)$"),
    limit: int = Query(default=200, ge=1, le=1000),
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    return {"items": await database.list_media_reports(status=status_filter, limit=limit), "status": status_filter}


@app.post("/api/admin/reports/{report_id}/resolve", dependencies=[Depends(require_admin)])
async def resolve_admin_report(
    report_id: int,
    payload: ReportResolutionPayload,
    principal: AccessPrincipal = Depends(require_admin),
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
    telegram: TeleBoxClient = Depends(get_telegram),
    cache: DiskCache = Depends(get_cache),
    indexer: MediaIndexer = Depends(get_indexer),
) -> dict[str, Any]:
    report = await database.get_media_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail={"code": "REPORT_NOT_FOUND"})
    account_id = str(report["account_id"])
    message_id = int(report["message_id"])
    if str(report.get("status")) not in {"open", "failed"}:
        raise HTTPException(status_code=409, detail={"code": "REPORT_ALREADY_RESOLVED"})
    reason = (payload.reason or "").strip()[:1000] or None
    if payload.resolution == "ignored" and (payload.media_action != "none" or payload.targets):
        # Reject an internally inconsistent moderation request before moving
        # every report in the group to the durable processing state.
        raise HTTPException(status_code=422, detail={"code": "IGNORED_REPORT_CANNOT_APPLY_ACTIONS"})
    reporters = await database.resolve_media_reports(
        account_id,
        message_id,
        status="processing",
        action=payload.media_action if payload.resolution == "actioned" else "ignored",
        reason=reason,
        resolved_by=principal.user_id,
    )
    target_results: list[dict[str, Any]] = []
    try:
        if payload.resolution != "ignored":
            if payload.media_action == "private":
                item = await database.review_media(
                    account_id,
                    message_id,
                    "revoked",
                    reason=reason or "举报受理后下架",
                    reviewed_by=str(principal.user_id or "admin"),
                )
                if not item:
                    raise HTTPException(status_code=404, detail={"code": "MEDIA_INDEX_NOT_FOUND"})
                await _sync_review_outbox_now(indexer)
            elif payload.media_action == "hidden":
                item = await database.set_media_hidden(account_id, message_id, True)
                if not item:
                    raise HTTPException(status_code=404, detail={"code": "MEDIA_INDEX_NOT_FOUND"})
            elif payload.media_action == "delete":
                await _delete_review_media(
                    account_id,
                    message_id,
                    reason=reason or "举报受理后删除",
                    deleted_by=str(principal.user_id or "admin"),
                    database=database,
                    telegram=telegram,
                    cache=cache,
                )
            for target_payload in payload.targets:
                target_results.append(
                    await _apply_sanction_target(
                        target_payload=target_payload,
                        principal=principal,
                        database=database,
                        auth=auth,
                        telegram=telegram,
                        cache=cache,
                    )
                )
        final_status = "ignored" if payload.resolution == "ignored" else "resolved"
        await database.resolve_media_reports(
            account_id,
            message_id,
            status=final_status,
            action=payload.media_action if payload.resolution == "actioned" else "ignored",
            reason=reason,
            resolved_by=principal.user_id,
        )
    except Exception as exc:
        await database.resolve_media_reports(
            account_id,
            message_id,
            status="failed",
            action=payload.media_action,
            reason=str(exc),
            resolved_by=principal.user_id,
        )
        raise

    reporter_ids = {int(item["reporter_user_id"]) for item in reporters}
    for reporter_user_id in reporter_ids:
        await database.create_notification(
            reporter_user_id,
            "report",
            "举报处理完成",
            "感谢你的反馈。管理员已完成核查。" if payload.resolution == "actioned" else "感谢你的反馈。管理员已完成核查，本次举报已作完结处理。",
            "/?view=square",
        )
    owner_user_id = report.get("owner_user_id")
    if owner_user_id and payload.resolution == "actioned" and payload.media_action != "none":
        labels = {"private": "下架并转为私人", "hidden": "隐藏", "delete": "删除"}
        await database.create_notification(
            int(owner_user_id),
            "media",
            "举报处置通知",
            f"你的资源“{report.get('media_title') or message_id}”已被管理员{labels.get(payload.media_action, payload.media_action)}。"
            + (f"理由：{reason}" if reason else ""),
            "/?view=my_public",
        )
    return {
        "ok": True,
        "status": "ignored" if payload.resolution == "ignored" else "resolved",
        "resolved_reports": len(reporters),
        "targets": target_results,
    }


@app.get("/api/admin/users/{user_id}/sanctions", dependencies=[Depends(require_admin)])
async def list_admin_user_sanctions(
    user_id: int,
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
) -> dict[str, Any]:
    if not await auth.get_user(user_id):
        raise HTTPException(status_code=404, detail={"code": "AUTH_USER_NOT_FOUND"})
    return {
        "items": await database.list_user_sanctions(user_id),
        "content_deletion_jobs": await database.list_content_deletion_jobs(target_user_id=user_id),
    }


@app.post("/api/admin/users/{user_id}/sanctions", dependencies=[Depends(require_admin)])
async def create_admin_user_sanctions(
    user_id: int,
    payload: SanctionTargetPayload,
    principal: AccessPrincipal = Depends(require_admin),
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
    telegram: TeleBoxClient = Depends(get_telegram),
    cache: DiskCache = Depends(get_cache),
) -> dict[str, Any]:
    if int(payload.user_id) != int(user_id):
        raise HTTPException(status_code=422, detail={"code": "SANCTION_TARGET_MISMATCH"})
    return await _apply_sanction_target(
        target_payload=payload,
        principal=principal,
        database=database,
        auth=auth,
        telegram=telegram,
        cache=cache,
    )


@app.delete("/api/admin/users/{user_id}/sanctions/{sanction_id}", dependencies=[Depends(require_admin)])
async def revoke_admin_user_sanction(
    user_id: int,
    sanction_id: int,
    principal: AccessPrincipal = Depends(require_admin),
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
) -> dict[str, Any]:
    target = await auth.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail={"code": "AUTH_USER_NOT_FOUND"})
    await _assert_can_moderate_user(principal, target, auth)
    sanction = await database.get_user_sanction(sanction_id)
    if not sanction or int(sanction["user_id"]) != int(user_id):
        raise HTTPException(status_code=404, detail={"code": "SANCTION_NOT_FOUND"})
    updated = await database.revoke_user_sanction(sanction_id, revoked_by=principal.user_id)
    await database.create_notification(user_id, "sanction", "处罚已解除", f"处罚 {sanction['sanction_type']} 已由管理员提前解除。", "/")
    return {"ok": True, "sanction": updated}


@app.post("/api/admin/users/{user_id}/content-deletion", dependencies=[Depends(require_admin)])
async def create_admin_content_deletion(
    user_id: int,
    payload: ContentDeletionPayload,
    principal: AccessPrincipal = Depends(require_admin),
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
    telegram: TeleBoxClient = Depends(get_telegram),
    cache: DiskCache = Depends(get_cache),
) -> dict[str, Any]:
    target = await auth.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail={"code": "AUTH_USER_NOT_FOUND"})
    await _assert_can_moderate_user(principal, target, auth)
    return await _schedule_content_deletion(
        target=target,
        reason=payload.reason,
        principal=principal,
        database=database,
        telegram=telegram,
        cache=cache,
    )


@app.get("/api/admin/content-deletion-jobs/{job_id}", dependencies=[Depends(require_admin)])
async def get_admin_content_deletion_job(
    job_id: str,
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    job = await database.get_content_deletion_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "CONTENT_DELETION_JOB_NOT_FOUND"})
    return job


@app.post("/api/admin/content-deletion-jobs/{job_id}/retry", dependencies=[Depends(require_admin)])
async def retry_admin_content_deletion_job(
    job_id: str,
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    cache: DiskCache = Depends(get_cache),
) -> dict[str, Any]:
    job = await database.get_content_deletion_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "CONTENT_DELETION_JOB_NOT_FOUND"})
    task = content_deletion_tasks.get(job_id)
    if task and not task.done():
        return job
    task = asyncio.create_task(_process_content_deletion_job(job_id, database, telegram, cache), name=f"content-deletion-{job_id}")
    content_deletion_tasks[job_id] = task
    task.add_done_callback(lambda _: content_deletion_tasks.pop(job_id, None))
    return job


@app.get("/api/admin/media/sync/status", dependencies=[Depends(require_admin)])
async def admin_media_sync_status(database: Database = Depends(get_database)) -> dict[str, list[dict]]:
    return {"items": await database.list_sync_states()}


@app.post("/api/admin/media/sync", dependencies=[Depends(require_admin)])
async def admin_media_sync(
    account: str | None = Query(default=None, min_length=1, max_length=40),
    full: bool = Query(default=False),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    indexer: MediaIndexer = Depends(get_indexer),
) -> dict:
    account_id = await telegram.resolve_account(account)
    group = await database.account_group_for_account(account_id)
    if group:
        account_id = str(group.get("active_account_id") or account_id)
    scheduled = indexer.schedule_sync(account_id, full=full)
    return {"scheduled": scheduled, "state": await database.get_sync_state(account_id)}


@app.get("/api/admin/media/review", dependencies=[Depends(require_admin)])
async def admin_media_review_queue(
    status_filter: str = Query(default="pending", alias="status", pattern="^(pending|approved|rejected|revoked|not_required|all)$"),
    account: str | None = Query(default=None, min_length=1, max_length=40),
    limit: int = Query(default=100, ge=1, le=1000),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    indexer: MediaIndexer = Depends(get_indexer),
) -> dict[str, Any]:
    account_id = None if not account or account in {"all", "*"} else await telegram.resolve_account(account)
    # A newly completed Helper Bot import may not have reached the periodic
    # reconciler yet.  Reconcile before reading the queue so the admin view is
    # not dependent on a manual refresh or a 60-second index pass.
    try:
        await indexer.reconcile_completed_ingest_jobs()
    except Exception:
        # The durable reconciler state/background loop will retry.  Existing
        # indexed rows should still be visible when TeleBox is temporarily
        # unavailable.
        pass
    items = await database.list_media_reviews(status=status_filter, account_id=account_id, limit=limit)
    for item in items:
        item_account = str(item["account_id"])
        item["thumbnail_url"] = (
            f"/api/media/{item['id']}/thumbnail?account={quote(item_account)}&size={item['size']}&v=review"
            if item.get("has_thumbnail") else None
        )
        item["stream_url"] = f"/api/media/{item['id']}/stream?account={quote(item_account)}"
    return {
        "items": items,
        "status": status_filter,
    }


async def _sync_review_outbox_now(indexer: MediaIndexer) -> None:
    try:
        await indexer.sync_review_outbox()
    except Exception:
        # The durable outbox remains for the background retry loop.
        return


async def _ban_review_submitters(
    submitters: set[tuple[str, str]],
    *,
    reason: str | None,
    database: Database,
    telegram: TeleBoxClient,
) -> None:
    """Disable Helper Bot and web access for review submitters."""
    for account_id, telegram_user_id in sorted(submitters):
        binding_updater = getattr(telegram, "set_binding_status", None)
        if binding_updater is not None:
            await binding_updater(
                telegram_user_id,
                enabled=False,
                banned=True,
                reason=reason,
            )
        else:
            # Keep compatibility with older test doubles/bridges while the
            # updated TeleBox container is rolled out.
            deleter = getattr(telegram, "delete_binding", None)
            if deleter is not None:
                await deleter(telegram_user_id)
        await database.ban_media_user(
            telegram_user_id,
            account_id,
            display_name=f"Telegram {telegram_user_id}",
        )


async def _notify_telegram_users(
    database: Database,
    submitters: Iterable[tuple[str, str | None]] | set[tuple[str, str | None]],
    kind: str,
    title: str,
    body: str,
    link: str | None = None,
) -> int:
    """Drop mailbox notifications for the web accounts of Telegram submitters."""
    sent = 0
    seen: set[str] = set()
    for _account_id, telegram_user_id in submitters:
        user_id = str(telegram_user_id or "").strip()
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        if await database.create_notification_for_telegram_user(user_id, kind, title, body, link):
            sent += 1
    return sent


async def _delete_review_media(
    account_id: str,
    message_id: int,
    *,
    reason: str | None,
    deleted_by: str,
    database: Database,
    telegram: TeleBoxClient,
    cache: DiskCache,
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    """Delete remote Telegram media, relay messages, cache and local rows."""
    targets = await database.media_review_targets(account_id, message_id, include_batch=True)
    if not targets:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_INDEX_NOT_FOUND"})
    clean_reason = (reason or "违规内容").strip()[:1000] or "违规内容"
    submitters = {
        (account_id, str(item["submitter_telegram_user_id"]))
        for item in targets
        if item.get("submitter_telegram_user_id")
    }
    for item in targets:
        job_id = item.get("source_ingest_job_id")
        if job_id is not None and hasattr(telegram, "delete_ingest_job"):
            await telegram.delete_ingest_job(
                int(job_id),
                reason=clean_reason,
                deleted_by=deleted_by,
            )
        elif hasattr(telegram, "delete_media"):
            await telegram.delete_media(
                account_id,
                int(item["id"]),
                reason=clean_reason,
                deleted_by=deleted_by,
            )
        else:
            raise HTTPException(status_code=503, detail={"code": "MEDIA_DELETE_UNAVAILABLE"})
        cache_key = telegram.media_cache_key(
            {"account_id": account_id, "id": int(item["id"])},
            {"size": int(item.get("size") or 0)},
        )
        await cache.delete_media(cache_key)
    deleted = await database.tombstone_media(
        account_id,
        message_id,
        reason=clean_reason,
        deleted_by=deleted_by,
        include_batch=True,
    )
    return deleted, submitters


async def _queue_replication_mutation(
    replication: DisasterRecoveryManager | None,
    account_id: str,
    message_id: int,
    action: str,
    *,
    caption: str | None = None,
) -> None:
    if not replication:
        return
    try:
        await replication.enqueue_mutation(account_id, int(message_id), action, caption=caption)
    except Exception:
        # Moderation must remain authoritative locally even if the replica
        # queue is temporarily unavailable; the durable queue will retry
        # already-created jobs independently.
        pass


async def _process_content_deletion_job(
    job_id: str,
    database: Database,
    telegram: TeleBoxClient,
    cache: DiskCache,
) -> None:
    await database.refresh_content_deletion_job(job_id, running=True)
    last_error: str | None = None
    cancelled = False
    try:
        for entry in await database.pending_content_deletion_items(job_id):
            account_id = str(entry["account_id"])
            message_id = int(entry["message_id"])
            current = await database.get_media_index(
                account_id,
                message_id,
                include_deleted=True,
                include_provenance=True,
            )
            if not current or current.get("deleted"):
                await database.update_content_deletion_item(
                    job_id, account_id, message_id, status="completed"
                )
                continue
            try:
                await _delete_review_media(
                    account_id,
                    message_id,
                    reason=str((await database.get_content_deletion_job(job_id) or {}).get("reason") or "管理员删除全部归属内容"),
                    deleted_by="admin-content-deletion",
                    database=database,
                    telegram=telegram,
                    cache=cache,
                )
                await database.update_content_deletion_item(
                    job_id, account_id, message_id, status="completed"
                )
            except Exception as exc:
                last_error = str(exc)
                await database.update_content_deletion_item(
                    job_id,
                    account_id,
                    message_id,
                    status="failed",
                    error=last_error,
                )
    except asyncio.CancelledError:
        cancelled = True
        await database.refresh_content_deletion_job(
            job_id,
            running=False,
            error="job cancelled by shutdown",
            cancelled=True,
        )
        raise
    finally:
        if not cancelled:
            await database.refresh_content_deletion_job(job_id, running=False, error=last_error)


async def _schedule_content_deletion(
    *,
    target: dict[str, Any],
    reason: str,
    principal: AccessPrincipal,
    database: Database,
    telegram: TeleBoxClient,
    cache: DiskCache,
) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    job = await database.create_content_deletion_job(
        job_id=job_id,
        target_user_id=int(target["id"]),
        telegram_user_id=str(target.get("telegram_user_id") or "") or None,
        reason=reason,
        created_by=principal.user_id,
    )
    task = asyncio.create_task(
        _process_content_deletion_job(job_id, database, telegram, cache),
        name=f"content-deletion-{job_id}",
    )
    content_deletion_tasks[job_id] = task
    task.add_done_callback(lambda _: content_deletion_tasks.pop(job_id, None))
    return job


async def _apply_sanction_target(
    *,
    target_payload: SanctionTargetPayload,
    principal: AccessPrincipal,
    database: Database,
    auth: AuthStore,
    telegram: TeleBoxClient,
    cache: DiskCache,
) -> dict[str, Any]:
    target = await auth.get_user(int(target_payload.user_id))
    if not target:
        raise HTTPException(status_code=404, detail={"code": "AUTH_USER_NOT_FOUND"})
    await _assert_can_moderate_user(principal, target, auth)
    created: list[dict[str, Any]] = []
    for sanction_payload in target_payload.sanctions:
        sanction = await database.create_user_sanction(
            user_id=int(target["id"]),
            sanction_type=sanction_payload.sanction_type,
            reason=sanction_payload.reason,
            expires_at=_validated_expiry(sanction_payload.expires_at),
            created_by=principal.user_id,
        )
        created.append(sanction)
        expiry_label = sanction.get("expires_at") or "永久"
        await database.create_notification(
            int(target["id"]),
            "sanction",
            "账号处罚通知",
            f"处罚类型：{sanction['sanction_type']}。理由：{sanction['reason']}。解除时间：{expiry_label}。",
            "/",
        )
        if sanction_payload.sanction_type == "login_ban":
            await auth.revoke_user_sessions(int(target["id"]))
            await auth.revoke_user_devices(int(target["id"]))
    if any(item.sanction_type in {"upload_mute", "login_ban"} for item in target_payload.sanctions):
        for job in await database.list_upload_jobs(500, owner_user_id=int(target["id"])):
            if str(job.get("status")) not in {"completed", "failed", "cancelled"}:
                await _cancel_upload(job, database, telegram)
        if target.get("telegram_user_id") and hasattr(telegram, "cancel_user_ingest_jobs"):
            try:
                await telegram.cancel_user_ingest_jobs(
                    str(target["telegram_user_id"]),
                    reason=created[-1]["reason"] if created else "账号受到上传限制",
                )
            except Exception:
                pass
    deletion_job = None
    if target_payload.delete_all_content:
        deletion_job = await _schedule_content_deletion(
            target=target,
            reason=created[-1]["reason"] if created else "管理员删除全部归属内容",
            principal=principal,
            database=database,
            telegram=telegram,
            cache=cache,
        )
    return {"user_id": int(target["id"]), "sanctions": created, "content_deletion_job": deletion_job}


@app.post("/api/admin/media/{message_id}/review", dependencies=[Depends(require_admin)])
async def review_media(
    message_id: int,
    payload: ReviewPayload,
    account: str | None = Query(default=None, min_length=1, max_length=40),
    database: Database = Depends(get_database),
    cache: DiskCache = Depends(get_cache),
    telegram: TeleBoxClient = Depends(get_telegram),
    indexer: MediaIndexer = Depends(get_indexer),
    replication: DisasterRecoveryManager = Depends(get_replication),
) -> dict[str, Any]:
    account_id = await telegram.resolve_account(account)
    targets = await database.media_review_targets(account_id, message_id, include_batch=True)
    if not targets:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_INDEX_NOT_FOUND"})
    submitters = {
        (account_id, str(item["submitter_telegram_user_id"]))
        for item in targets
        if item.get("submitter_telegram_user_id")
    }
    if payload.ban_submitter:
        await _ban_review_submitters(
            submitters,
            reason=payload.reason,
            database=database,
            telegram=telegram,
        )
    if payload.decision == "deleted":
        deleted, _ = await _delete_review_media(
            account_id,
            message_id,
            reason=payload.reason,
            deleted_by="admin",
            database=database,
            telegram=telegram,
            cache=cache,
        )
        await _queue_replication_mutation(replication, account_id, message_id, "delete")
        await _notify_telegram_users(
            database,
            submitters,
            "media",
            "资源已被删除",
            f"管理员已删除你提交的资源（{(payload.reason or '').strip() or '违反平台规则'}）。",
        )
        return {
            "decision": "deleted",
            "deleted": len(deleted),
            "items": deleted,
            "submitters_banned": len(submitters) if payload.ban_submitter else 0,
        }
    item = await database.review_media(
        account_id,
        message_id,
        payload.decision,
        reason=payload.reason,
        reviewed_by="admin",
    )
    if not item:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_INDEX_NOT_FOUND"})
    await _sync_review_outbox_now(indexer)
    await _queue_replication_mutation(
        replication,
        account_id,
        message_id,
        "public" if payload.decision == "approved" else "private" if payload.decision in {"rejected", "revoked"} else "private",
    )
    item_title = str(item.get("title") or item.get("filename") or message_id)
    reason_text = (payload.reason or "").strip() or "未说明原因"
    if payload.decision == "approved":
        await _notify_telegram_users(database, submitters, "media", "公开申请已通过", f"你提交的资源已通过管理员审核并公开：{item_title}")
    elif payload.decision == "rejected":
        await _notify_telegram_users(database, submitters, "media", "公开申请未通过", f"你提交的资源未通过审核（{reason_text}）：{item_title}")
    elif payload.decision == "revoked":
        await _notify_telegram_users(database, submitters, "media", "资源已设为私有", f"管理员已将你的资源设为私有（仅管理员和上传者可见）：{item_title}")
    return item


@app.delete("/api/admin/media/{message_id}", dependencies=[Depends(require_admin)])
async def delete_admin_media(
    message_id: int,
    payload: DeleteMediaPayload,
    account: str | None = Query(default=None, min_length=1, max_length=40),
    database: Database = Depends(get_database),
    cache: DiskCache = Depends(get_cache),
    telegram: TeleBoxClient = Depends(get_telegram),
    replication: DisasterRecoveryManager = Depends(get_replication),
) -> dict[str, Any]:
    account_id = await telegram.resolve_account(account)
    targets = await database.media_review_targets(account_id, message_id, include_batch=True)
    if not targets:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_INDEX_NOT_FOUND"})
    submitters = {
        (account_id, str(item["submitter_telegram_user_id"]))
        for item in targets
        if item.get("submitter_telegram_user_id")
    }
    if payload.ban_submitter:
        await _ban_review_submitters(
            submitters,
            reason=payload.reason,
            database=database,
            telegram=telegram,
        )
    deleted, _ = await _delete_review_media(
        account_id,
        message_id,
        reason=payload.reason,
        deleted_by="admin",
        database=database,
        telegram=telegram,
        cache=cache,
    )
    await _queue_replication_mutation(replication, account_id, message_id, "delete")
    await _notify_telegram_users(
        database,
        submitters,
        "media",
        "资源已被删除",
        f"管理员已删除你提交的资源（{(payload.reason or '').strip() or '违反平台规则'}）。",
    )
    return {
        "decision": "deleted",
        "deleted": len(deleted),
        "items": deleted,
        "submitters_banned": len(submitters) if payload.ban_submitter else 0,
    }


@app.post("/api/admin/media/review/bulk", dependencies=[Depends(require_admin)])
async def review_media_bulk(
    payload: BulkReviewPayload,
    database: Database = Depends(get_database),
    cache: DiskCache = Depends(get_cache),
    telegram: TeleBoxClient = Depends(get_telegram),
    indexer: MediaIndexer = Depends(get_indexer),
    replication: DisasterRecoveryManager = Depends(get_replication),
) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    for entry in payload.items:
        targets.extend(
            await database.media_review_targets(
                str(entry["account_id"]),
                int(entry["message_id"]),
                include_batch=True,
            )
        )
    submitters = {
        (str(item["account_id"]), str(item["submitter_telegram_user_id"]))
        for item in targets
        if item.get("submitter_telegram_user_id")
    }
    if payload.ban_submitter:
        await _ban_review_submitters(
            submitters,
            reason=payload.reason,
            database=database,
            telegram=telegram,
        )
    if payload.decision == "deleted":
        deleted_ids: set[tuple[str, int]] = set()
        deleted_items: list[dict[str, Any]] = []
        for entry in payload.items:
            account_id = str(entry["account_id"])
            message_id = int(entry["message_id"])
            batch_targets = await database.media_review_targets(account_id, message_id, include_batch=True)
            for target in batch_targets:
                key = (account_id, int(target["id"]))
                if key in deleted_ids:
                    continue
                deleted_ids.add(key)
                # The tombstone method groups by review_batch_id, so calling
                # it on the first item deletes the complete media group.
                deleted, _ = await _delete_review_media(
                    account_id,
                    int(target["id"]),
                    reason=payload.reason,
                    deleted_by="admin",
                    database=database,
                    telegram=telegram,
                    cache=cache,
                )
                await _queue_replication_mutation(replication, account_id, int(target["id"]), "delete")
                deleted_items.extend(deleted)
                deleted_ids.update((account_id, int(row["id"])) for row in batch_targets)
                break
        await _notify_telegram_users(
            database,
            submitters,
            "media",
            "资源已被删除",
            f"管理员已删除你提交的 {len(payload.items)} 项资源（{(payload.reason or '').strip() or '违反平台规则'}）。",
        )
        return {
            "updated": len(deleted_items),
            "deleted": len(deleted_items),
            "items": deleted_items,
            "decision": "deleted",
            "submitters_banned": len(submitters) if payload.ban_submitter else 0,
        }
    items = await database.review_media_bulk(
        [item.model_dump() for item in payload.items],
        payload.decision,
        reason=payload.reason,
        reviewed_by="admin",
    )
    await _sync_review_outbox_now(indexer)
    for entry in payload.items:
        await _queue_replication_mutation(
            replication,
            str(entry["account_id"]),
            int(entry["message_id"]),
            "public" if payload.decision == "approved" else "private",
        )
    labels = {"approved": "通过并公开", "rejected": "未通过", "revoked": "已设为私有"}
    await _notify_telegram_users(
        database,
        submitters,
        "media",
        "资源审核结果",
        f"你提交的 {len(payload.items)} 项资源已完成审核：{labels.get(payload.decision, payload.decision)}。"
        + (f"理由：{(payload.reason or '').strip()}" if payload.reason else ""),
    )
    return {"updated": len(items), "items": items, "decision": payload.decision}


@app.patch("/api/admin/media/{message_id}/visibility", dependencies=[Depends(require_admin)])
async def update_media_visibility(
    message_id: int,
    payload: VisibilityPayload,
    account: str | None = Query(default=None, min_length=1, max_length=40),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    indexer: MediaIndexer = Depends(get_indexer),
    replication: DisasterRecoveryManager = Depends(get_replication),
) -> dict:
    account_id = await telegram.resolve_account(account)
    current = await database.get_media_index(account_id, message_id)
    if not current:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_INDEX_NOT_FOUND"})
    if payload.visibility == "hidden":
        item = await database.set_media_hidden(account_id, message_id, True)
    elif payload.visibility == "private" and bool(current.get("hidden")):
        item = await database.set_media_hidden(account_id, message_id, False)
    else:
        item = await database.review_media(
            account_id,
            message_id,
            "approved" if payload.visibility == "public" else "revoked",
            reason="管理员通过兼容入口更新媒体可见性",
            reviewed_by="admin",
        )
    if not item:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_INDEX_NOT_FOUND"})
    await _sync_review_outbox_now(indexer)
    await _queue_replication_mutation(
        replication,
        account_id,
        message_id,
        "hide" if payload.visibility == "hidden" else "private" if payload.visibility == "private" else "public",
    )
    item_title = str(item.get("title") or item.get("filename") or message_id)
    submitter = item.get("submitter_telegram_user_id")
    if payload.visibility == "hidden":
        await _notify_telegram_users(database, {(account_id, submitter)}, "media", "资源已被隐藏", f"管理员已将你的资源隐藏（仅管理员可见）：{item_title}")
    elif payload.visibility == "private":
        await _notify_telegram_users(database, {(account_id, submitter)}, "media", "资源已设为私有", f"管理员已将你的资源设为私有（仅管理员和上传者可见）：{item_title}")
    else:
        await _notify_telegram_users(database, {(account_id, submitter)}, "media", "资源已公开", f"管理员已将你的资源公开：{item_title}")
    return item


@app.post("/api/admin/media/visibility", dependencies=[Depends(require_admin)])
async def update_media_visibility_bulk(
    payload: BulkVisibilityPayload,
    database: Database = Depends(get_database),
    indexer: MediaIndexer = Depends(get_indexer),
    replication: DisasterRecoveryManager = Depends(get_replication),
) -> dict[str, int | str]:
    entries = [item.model_dump() for item in payload.items]
    submitters: set[tuple[str, str | None]] = set()
    for entry in entries:
        for target in await database.media_review_targets(
            str(entry["account_id"]), int(entry["message_id"]), include_batch=False
        ):
            submitters.add((str(entry["account_id"]), target.get("submitter_telegram_user_id")))
    if payload.visibility == "hidden":
        updated = await database.set_media_hidden_bulk(entries, True)
    elif payload.visibility == "private":
        hidden_targets: list[dict[str, Any]] = []
        visible_targets: list[dict[str, Any]] = []
        for entry in entries:
            current = await database.get_media_index(str(entry["account_id"]), int(entry["message_id"]))
            if current and bool(current.get("hidden")):
                hidden_targets.append(entry)
            else:
                visible_targets.append(entry)
        restored = await database.set_media_hidden_bulk(hidden_targets, False)
        reviewed = await database.review_media_bulk(
            visible_targets,
            "revoked",
            reason="管理员通过兼容入口批量更新媒体可见性",
            reviewed_by="admin",
        )
        updated = restored + len(reviewed)
    else:
        reviewed = await database.review_media_bulk(
            entries,
            "approved",
            reason="管理员通过兼容入口批量更新媒体可见性",
            reviewed_by="admin",
        )
        updated = len(reviewed)
    await _sync_review_outbox_now(indexer)
    for entry in entries:
        await _queue_replication_mutation(
            replication,
            str(entry["account_id"]),
            int(entry["message_id"]),
            "hide" if payload.visibility == "hidden" else "private" if payload.visibility == "private" else "public",
        )
    count = len(entries)
    if payload.visibility == "hidden":
        await _notify_telegram_users(database, submitters, "media", "资源已被隐藏", f"管理员已隐藏你提交的 {count} 项资源（仅管理员可见）。")
    elif payload.visibility == "private":
        await _notify_telegram_users(database, submitters, "media", "资源已设为私有", f"管理员已将你提交的 {count} 项资源设为私有（仅管理员和上传者可见）。")
    else:
        await _notify_telegram_users(database, submitters, "media", "资源已公开", f"管理员已将你提交的 {count} 项资源公开。")
    return {"updated": updated, "visibility": payload.visibility}


# ----------------------------------------------------------------------
# Folders
# ----------------------------------------------------------------------


@app.get("/api/folders", dependencies=[Depends(require_viewer)])
async def list_folders(
    database: Database = Depends(get_database),
    principal: AccessPrincipal = Depends(require_media_access),
) -> dict[str, Any]:
    folders = await database.list_folders(
        owner_user_id=None if principal.is_admin else principal.user_id,
        owner_telegram_user_id=None if principal.is_admin else principal.telegram_user_id,
        include_hidden=principal.is_admin,
    )
    return {"items": folders}


@app.post("/api/admin/folders", dependencies=[Depends(require_admin)])
async def create_folder(
    payload: FolderPayload,
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    try:
        folder = await database.create_folder(payload.name, parent_id=payload.parent_id, created_by="admin")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "FOLDER_CONFLICT", "message": str(exc)}) from exc
    return folder


@app.patch("/api/admin/folders/{folder_id}", dependencies=[Depends(require_admin)])
async def update_folder(
    folder_id: int,
    payload: FolderRenamePayload,
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    if payload.parent_id is not None and payload.name is None:
        try:
            folder = await database.move_folder(folder_id, payload.parent_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail={"code": "FOLDER_CONFLICT", "message": str(exc)}) from exc
        if not folder:
            raise HTTPException(status_code=404, detail={"code": "FOLDER_NOT_FOUND"})
        return folder
    try:
        folder = await database.rename_folder(folder_id, payload.name or "")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "FOLDER_CONFLICT", "message": str(exc)}) from exc
    if not folder:
        raise HTTPException(status_code=404, detail={"code": "FOLDER_NOT_FOUND"})
    return folder


@app.delete("/api/admin/folders/{folder_id}", dependencies=[Depends(require_admin)])
async def delete_folder(
    folder_id: int,
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    deleted = await database.delete_folder(folder_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={"code": "FOLDER_NOT_FOUND"})
    return {"ok": True, "deleted": deleted}


@app.put("/api/admin/folders/{folder_id}/items", dependencies=[Depends(require_admin)])
async def add_folder_items(
    folder_id: int,
    payload: FolderItemsPayload,
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    try:
        added = await database.set_folder_items(folder_id, [item.model_dump() for item in payload.items])
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "FOLDER_NOT_FOUND", "message": str(exc)}) from exc
    return {"ok": True, "added": added}


@app.delete("/api/admin/folders/{folder_id}/items", dependencies=[Depends(require_admin)])
async def remove_folder_items(
    folder_id: int,
    payload: FolderItemsPayload,
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    removed = await database.remove_folder_items(folder_id, [item.model_dump() for item in payload.items])
    return {"ok": True, "removed": removed}


# ----------------------------------------------------------------------
# Notifications (mailbox)
# ----------------------------------------------------------------------


async def require_mailbox_principal(
    principal: AccessPrincipal | None = Depends(optional_access_principal),
) -> AccessPrincipal:
    if principal and principal.user_id is not None:
        return principal
    raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})


@app.get("/api/notifications")
async def list_my_notifications(
    limit: int = Query(default=30, ge=1, le=100),
    cursor: int | None = Query(default=None, ge=1),
    database: Database = Depends(get_database),
    principal: AccessPrincipal = Depends(require_mailbox_principal),
) -> dict[str, Any]:
    items, next_cursor, has_more = await database.list_notifications(
        int(principal.user_id), limit=limit, cursor=cursor
    )
    return {
        "items": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "unread": await database.unread_notification_count(int(principal.user_id)),
    }


@app.get("/api/notifications/unread-count")
async def unread_notification_count(
    database: Database = Depends(get_database),
    principal: AccessPrincipal = Depends(require_mailbox_principal),
) -> dict[str, int]:
    return {"count": await database.unread_notification_count(int(principal.user_id))}


@app.post("/api/notifications/read")
async def mark_notifications_read(
    payload: NotificationReadPayload,
    database: Database = Depends(get_database),
    principal: AccessPrincipal = Depends(require_mailbox_principal),
) -> dict[str, Any]:
    updated = await database.mark_notifications_read(
        int(principal.user_id), payload.ids if not payload.all else None
    )
    return {"ok": True, "updated": updated}


@app.delete("/api/notifications")
async def delete_my_notifications(
    payload: NotificationDeletePayload,
    database: Database = Depends(get_database),
    principal: AccessPrincipal = Depends(require_mailbox_principal),
) -> dict[str, Any]:
    removed = await database.delete_notifications(
        int(principal.user_id), payload.ids if not payload.all else None
    )
    return {"ok": True, "removed": removed}


@app.post("/api/admin/notifications", dependencies=[Depends(require_admin)])
async def send_admin_notification(
    payload: AdminNotificationPayload,
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
) -> dict[str, Any]:
    if payload.user_id is not None:
        user = await auth.get_user(int(payload.user_id))
        if not user:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
        notification = await database.create_notification(
            int(payload.user_id), payload.kind, payload.title, payload.body, payload.link
        )
        return {"ok": True, "sent": 1, "notification": notification}
    sent = await database.create_notification_broadcast(
        payload.kind, payload.title, payload.body, payload.link
    )
    return {"ok": True, "sent": sent}


@app.get("/api/admin/notifications", dependencies=[Depends(require_admin)])
async def list_admin_notifications(
    limit: int = Query(default=100, ge=1, le=500),
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    return {"items": await database.list_notifications_admin(limit)}




# ----------------------------------------------------------------------
# Deployment backups (host /backups directory, managed by deploy.ps1)
# ----------------------------------------------------------------------


@app.get("/api/admin/backups", dependencies=[Depends(require_admin)])
async def admin_backups() -> dict[str, Any]:
    """List the deployment backups created by deploy.ps1."""
    return list_backups(settings.backups_dir)


@app.delete("/api/admin/backups/{stamp}", dependencies=[Depends(require_admin)])
async def admin_delete_backup(stamp: str) -> dict[str, Any]:
    try:
        return {"ok": True, "deleted": delete_backup(settings.backups_dir, stamp)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_BACKUP_STAMP", "message": str(exc)}) from exc
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=404, detail={"code": "BACKUP_NOT_FOUND", "message": str(exc)}) from exc


@app.post("/api/admin/backups/cleanup", dependencies=[Depends(require_admin)])
async def admin_backup_cleanup(payload: BackupCleanupPayload) -> dict[str, Any]:
    try:
        return cleanup_backups(settings.backups_dir, payload.keep, dry_run=payload.dry_run)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail={"code": "BACKUP_CLEANUP_FAILED", "message": str(exc)}) from exc


# ----------------------------------------------------------------------
# SavedStream system configuration backups (Telegram disaster recovery)
# ----------------------------------------------------------------------


@app.get("/api/admin/system-backups/settings", dependencies=[Depends(require_admin)])
async def admin_system_backup_settings(database: Database = Depends(get_database)) -> dict[str, Any]:
    return _system_backup_public_settings(await database.get_system_backup_settings())


@app.put("/api/admin/system-backups/settings", dependencies=[Depends(require_admin)])
async def update_admin_system_backup_settings(
    payload: SystemBackupSettingsPayload,
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    try:
        next_run = next_cron(payload.cron_expr, payload.timezone).isoformat() if payload.enabled else None
    except SystemBackupError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_BACKUP_SCHEDULE", "message": str(exc)}) from exc
    values: dict[str, Any] = {
        "enabled": 1 if payload.enabled else 0,
        "cron_expr": payload.cron_expr.strip(),
        "timezone": payload.timezone.strip(),
        "account_id": payload.account_id.strip() if payload.account_id else None,
        "next_run_at": next_run,
        "last_error": None,
    }
    if payload.clear_passphrase:
        values.update({"passphrase_salt": None, "passphrase_nonce": None, "passphrase_ciphertext": None})
    elif payload.passphrase:
        try:
            wrapped = wrap_passphrase(payload.passphrase, settings.admin_key)
        except SystemBackupError as exc:
            raise HTTPException(status_code=409, detail={"code": "ADMIN_KEY_REQUIRED_FOR_BACKUP_PASSWORD", "message": str(exc)}) from exc
        values.update({
            "passphrase_salt": wrapped["salt"],
            "passphrase_nonce": wrapped["nonce"],
            "passphrase_ciphertext": wrapped["ciphertext"],
        })
    return _system_backup_public_settings(await database.update_system_backup_settings(values))


@app.get("/api/admin/system-backups", dependencies=[Depends(require_admin)])
async def list_admin_system_backups(
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    return {"items": await database.list_system_backups()}


@app.get("/api/admin/system-backups/jobs/{job_id}", dependencies=[Depends(require_admin)])
async def get_admin_system_backup_job(job_id: str, database: Database = Depends(get_database)) -> dict[str, Any]:
    job = await database.get_system_backup_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "BACKUP_JOB_NOT_FOUND"})
    return job


@app.post("/api/admin/system-backups/run", dependencies=[Depends(require_admin)])
async def run_admin_system_backup(
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    indexer: MediaIndexer = Depends(get_indexer),
    principal: AccessPrincipal = Depends(require_admin),
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    await database.create_system_backup_job({
        "id": job_id, "backup_id": None, "trigger": "manual", "status": "queued", "phase": "queued",
        "progress": 0, "attempts": 0, "temp_path": None, "error": None,
        "created_by": principal.user_id, "created_at": utc_now(), "updated_at": utc_now(), "completed_at": None,
    })
    task = asyncio.create_task(_run_system_backup_job(job_id, trigger="manual", database=database, telegram=telegram, indexer=indexer, created_by=principal.user_id), name=f"system-backup-{job_id}")
    system_backup_tasks[job_id] = task
    return {"job_id": job_id, "status": "queued"}


@app.post("/api/admin/system-backups/import", dependencies=[Depends(require_admin)])
async def import_admin_system_backup(
    file: UploadFile = File(...),
    passphrase: str | None = Query(default=None, min_length=1, max_length=512),
    passphrase_form: str | None = Form(default=None, min_length=1, max_length=512),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    indexer: MediaIndexer = Depends(get_indexer),
    principal: AccessPrincipal = Depends(require_admin),
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    staging = settings.data_dir / "system-backup-staging" / job_id
    archive_path = staging / (Path(file.filename or "uploaded.ssbak").name or "uploaded.ssbak")
    if archive_path.suffix.lower() != ".ssbak":
        raise HTTPException(status_code=422, detail={"code": "INVALID_BACKUP_FILENAME"})
    try:
        await _copy_upload_to_path(file, archive_path)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    backup_id = str(uuid.uuid4())
    await database.create_system_backup({
        "id": backup_id, "filename": archive_path.name, "source": "upload", "status": "importing",
        "created_at": utc_now(), "size_bytes": archive_size(archive_path), "sha256": "", "account_id": None,
        "message_id": None, "manifest_json": "{}", "error": None, "imported_at": None,
    })
    try:
        effective_passphrase = passphrase_form or passphrase or await _stored_system_backup_passphrase(database)
    except SystemBackupError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(status_code=422, detail={"code": "BACKUP_PASSPHRASE_REQUIRED", "message": str(exc)}) from exc
    await database.create_system_backup_job({
        "id": job_id, "backup_id": backup_id, "trigger": "upload", "status": "queued", "phase": "queued",
        "progress": 0, "attempts": 0, "temp_path": str(archive_path), "error": None,
        "created_by": principal.user_id, "created_at": utc_now(), "updated_at": utc_now(), "completed_at": None,
    })
    task = asyncio.create_task(_restore_system_backup_archive(job_id, archive_path, database=database, telegram=telegram, indexer=indexer, passphrase=effective_passphrase, backup_id=backup_id), name=f"system-restore-{job_id}")
    system_backup_tasks[job_id] = task
    return {"job_id": job_id, "backup_id": backup_id, "status": "queued"}


@app.post("/api/admin/system-backups/scan-telegram", dependencies=[Depends(require_admin)])
async def scan_admin_system_backups(
    account_id: str | None = Query(default=None, max_length=40),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict[str, Any]:
    requested_account = str(account_id or "").strip()
    if not requested_account:
        backup_settings = await database.get_system_backup_settings()
        requested_account = str(backup_settings.get("account_id") or settings.telebox_default_account).strip()
    account = await telegram.resolve_account(requested_account)
    items = await telegram.list_system_backups(account_id=account)
    created = 0
    for item in items:
        try:
            message_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if message_id <= 0:
            continue
        existing = await database.get_system_backup_by_telegram_message(account, message_id)
        stable_backup_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"savedstream:{account}:{message_id}"))
        if not existing:
            existing = await database.get_system_backup(stable_backup_id)
        backup_id = str(existing["id"]) if existing else stable_backup_id
        if existing:
            try:
                manifest_json = json.loads(str(existing.get("manifest_json") or "{}"))
            except (TypeError, ValueError):
                manifest_json = {}
            if not isinstance(manifest_json, dict):
                manifest_json = {}
            manifest_json["telegram"] = item
            manifest_json.setdefault("marker", BACKUP_MARKER)
        else:
            manifest_json = {"marker": BACKUP_MARKER, "telegram": item}
        await database.create_system_backup({
            "id": backup_id,
            "filename": str(item.get("filename") or f"telegram-{item.get('id')}.ssbak"),
            "source": str(existing.get("source") or "telegram") if existing else "telegram",
            "status": "available",
            "created_at": (
                str(existing.get("created_at") or item.get("date") or utc_now())
                if existing
                else str(item.get("date") or utc_now())
            ),
            "size_bytes": int(item.get("size") or 0),
            "sha256": str(existing.get("sha256") or "") if existing else "",
            "account_id": account,
            "message_id": message_id,
            "manifest_json": json.dumps(manifest_json, ensure_ascii=False),
            "error": None,
            "imported_at": existing.get("imported_at") if existing else None,
        })
        created += 0 if existing else 1
    return {"account_id": account, "items": await database.list_system_backups(), "discovered": created}


@app.post("/api/admin/system-backups/{backup_id}/restore", dependencies=[Depends(require_admin)])
async def restore_admin_system_backup(
    backup_id: str,
    passphrase: str | None = Query(default=None, min_length=1, max_length=512),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    indexer: MediaIndexer = Depends(get_indexer),
    principal: AccessPrincipal = Depends(require_admin),
) -> dict[str, Any]:
    backup = await database.get_system_backup(backup_id)
    if not backup:
        raise HTTPException(status_code=404, detail={"code": "SYSTEM_BACKUP_NOT_FOUND"})
    if not backup.get("account_id") or not backup.get("message_id"):
        raise HTTPException(status_code=409, detail={"code": "SYSTEM_BACKUP_NOT_LINKED_TO_TELEGRAM"})
    try:
        effective_passphrase = passphrase or await _stored_system_backup_passphrase(database)
    except SystemBackupError as exc:
        raise HTTPException(status_code=422, detail={"code": "BACKUP_PASSPHRASE_REQUIRED", "message": str(exc)}) from exc
    job_id = str(uuid.uuid4())
    staging = settings.data_dir / "system-backup-staging" / job_id
    archive_path = staging / str(backup.get("filename") or f"{backup_id}.ssbak")
    staging.mkdir(parents=True, exist_ok=True)
    with archive_path.open("wb") as handle:
        message = {"account_id": backup["account_id"], "id": int(backup["message_id"])}
        offset = 0
        expected = max(1, int(backup.get("size_bytes") or 0))
        try:
            _message, remote_item = await telegram.get_media_message(str(backup["account_id"]), int(backup["message_id"]))
            expected = max(expected, int(remote_item.get("size") or 0))
        except Exception:
            pass
        while offset < expected:
            chunk = await telegram.download_chunk(message, offset, expected)
            if not chunk:
                break
            handle.write(chunk)
            offset += len(chunk)
    await database.create_system_backup_job({
        "id": job_id, "backup_id": backup_id, "trigger": "telegram", "status": "queued", "phase": "queued",
        "progress": 0, "attempts": 0, "temp_path": str(archive_path), "error": None,
        "created_by": principal.user_id, "created_at": utc_now(), "updated_at": utc_now(), "completed_at": None,
    })
    task = asyncio.create_task(_restore_system_backup_archive(job_id, archive_path, database=database, telegram=telegram, indexer=indexer, passphrase=effective_passphrase, backup_id=backup_id), name=f"system-restore-{job_id}")
    system_backup_tasks[job_id] = task
    return {"job_id": job_id, "backup_id": backup_id, "status": "queued"}




# ----------------------------------------------------------------------
# Storage awareness (disk usage, alerts and optimization suggestions)
# ----------------------------------------------------------------------


@app.get("/api/admin/storage", dependencies=[Depends(require_admin)])
async def admin_storage(
    database: Database = Depends(get_database),
    cache: DiskCache = Depends(get_cache),
) -> dict[str, Any]:
    """Disk usage snapshot with alerts and optimization recommendations."""
    cache_stats = await cache.stats()
    try:
        database_bytes = settings.database_path.stat().st_size
    except OSError:
        database_bytes = 0
    return storage_snapshot(
        cache_bytes=int(cache_stats.get("bytes") or 0),
        cache_files=int(cache_stats.get("files") or 0),
        cache_limit_bytes=await database.get_cache_limit(),
        database_bytes=database_bytes,
        backups=list_backups(settings.backups_dir),
    )


async def _process_upload_job(
    job_id: str,
    database: Database,
    telegram: TeleBoxClient,
    indexer: MediaIndexer,
    replication: DisasterRecoveryManager | None = None,
) -> None:
    job = await database.get_upload_job(job_id)
    if not job:
        return
    if str(job.get("status")) in {"completed", "failed", "cancelled"}:
        return
    temp_path = Path(str(job["temp_path"]))
    try:
        await database.update_upload_job(job_id, status="running", phase="telegram_upload", progress=0, error=None)
        last_update = 0

        async def on_progress(sent: int, total: int) -> None:
            nonlocal last_update
            if sent != total and sent - last_update < 4 * 1024 * 1024:
                return
            last_update = sent
            await database.update_upload_job(
                job_id,
                progress=round((sent / max(1, total)) * 100, 2),
                bytes_sent=sent,
            )

        item = await telegram.upload_file(
            account_id=str(job["account_id"]),
            file_path=temp_path,
            filename=str(job["filename"]),
            mime_type=str(job["mime_type"]),
            progress_callback=on_progress,
        )
        reservation_key = str(job.get("quota_reservation_key") or "")
        if reservation_key and hasattr(telegram, "complete_upload_quota"):
            await telegram.complete_upload_quota(reservation_key)
            await database.update_upload_job(job_id, quota_reservation_key=None)
        current = await database.get_upload_job(job_id)
        if not current or str(current.get("status")) == "cancelled":
            # The Telegram upload may have reached Saved Messages just before
            # cancellation.  Do not report it as a completed web upload; the
            # regular indexer will reconcile the remote message later.
            return
        item["account_id"] = str(job["account_id"])
        await database.update_upload_job(job_id, phase="indexing", progress=99.5)
        current = await database.get_upload_job(job_id)
        if not current or str(current.get("status")) == "cancelled":
            return
        requested_visibility = str(job.get("requested_visibility") or "private")
        review_status = str(job.get("review_status") or ("pending" if requested_visibility == "public" else "not_required"))
        effective_visibility = "public" if requested_visibility == "public" and review_status == "approved" else "private"
        await database.upsert_media_index(
            item,
            visibility=effective_visibility,
            submitter_telegram_user_id=str(job.get("submitter_telegram_user_id") or "") or None,
            owner_user_id=int(job["owner_user_id"]) if job.get("owner_user_id") is not None else None,
            requested_visibility=requested_visibility,
            review_status=review_status,
            # Web batches are for upload progress/ownership only.  They are
            # deliberately not review batches because each dropped file can
            # choose an independent visibility and moderation decision.
            review_batch_id=None,
            upload_source=str(job.get("upload_source") or "web"),
            upload_batch_id=str(job.get("batch_id") or "") or None,
            account_group_id=str(job.get("account_group_id") or "") or None,
        )
        if job.get("folder_id") is not None:
            await database.set_folder_items(
                int(job["folder_id"]),
                [{"account_id": str(job["account_id"]), "message_id": int(item["id"])}],
            )
        else:
            await database.rebuild_timeline(str(job["account_id"]))
        await database.complete_upload_job(job_id, message_id=int(item["id"]))
        if replication:
            try:
                await replication.enqueue_media(str(job["account_id"]), int(item["id"]))
            except Exception:
                # Replication is durable/best-effort and must not make the
                # primary upload fail after Telegram has accepted it.
                pass
        # Make the item visible to the next local query immediately, while the
        # normal background worker will reconcile it on its next pass.
        indexer.schedule_sync(str(job["account_id"]), full=False)
    except asyncio.CancelledError:
        reservation_key = str(job.get("quota_reservation_key") or "")
        if reservation_key and hasattr(telegram, "release_upload_quota"):
            try:
                await telegram.release_upload_quota(reservation_key)
            except Exception:
                pass
        await database.update_upload_job(job_id, status="cancelled", phase="cancelled", error="upload cancelled")
        raise
    except Exception as exc:
        reservation_key = str(job.get("quota_reservation_key") or "")
        current = await database.get_upload_job(job_id)
        if reservation_key and current and current.get("quota_reservation_key") and hasattr(telegram, "release_upload_quota"):
            try:
                await telegram.release_upload_quota(reservation_key)
            except Exception:
                pass
        await database.update_upload_job(job_id, status="failed", phase="failed", error=str(exc), temp_path=None)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _public_upload_job(job: dict | None) -> dict:
    if not job:
        return {}
    return {
        key: value
        for key, value in job.items()
        if key not in {"temp_path", "quota_reservation_key"}
    }


def _public_traffic_settings(raw: dict) -> dict:
    capacity = int(raw.get("monthly_capacity_bytes", 0))
    limit = int(raw.get("monthly_limit_bytes", 0))
    return {
        "enabled": bool(int(raw.get("enabled", 0))),
        "monthly_capacity_bytes": capacity,
        "monthly_limit_bytes": limit,
        "monthly_capacity_gb": round(capacity / 1000**3, 3),
        "monthly_limit_gb": round(limit / 1000**3, 3),
        "warning_percent": int(raw.get("warning_percent", 80)),
        "admin_bypass": bool(int(raw.get("admin_bypass", 0))),
        "timezone": str(raw.get("timezone", "UTC")),
        "updated_at": raw.get("updated_at"),
    }


async def _traffic_summary(database: Database, traffic: TrafficController) -> dict:
    raw_settings = await database.get_traffic_settings()
    settings_public = _public_traffic_settings(raw_settings)
    usage = await database.get_traffic_usage("month")
    snapshot = await traffic.snapshot()
    used = int(usage["bytes_in"]) + int(usage["bytes_out"])
    limit = int(raw_settings.get("monthly_limit_bytes", 0))
    return {
        "settings": settings_public,
        "usage": {
            **usage,
            "bytes_total": used,
            "remaining_bytes": max(0, limit - used) if settings_public["enabled"] else None,
            "usage_percent": round((used / limit) * 100, 2) if limit else 0,
        },
        "active_requests": snapshot.active_requests,
        "active_streams": snapshot.active_streams,
        "active_uploads": snapshot.active_uploads,
        "inbound_bps": snapshot.inbound_bps,
        "outbound_bps": snapshot.outbound_bps,
    }


@app.get("/api/admin/traffic/summary", dependencies=[Depends(require_admin)])
async def admin_traffic_summary(
    database: Database = Depends(get_database),
    traffic: TrafficController = Depends(get_traffic),
) -> dict:
    return await _traffic_summary(database, traffic)


@app.get("/api/admin/traffic/series", dependencies=[Depends(require_admin)])
async def admin_traffic_series(
    range_name: str = Query(default="7d", alias="range", pattern="^(7d|30d|month)$"),
    database: Database = Depends(get_database),
) -> dict:
    return {"range": range_name, "items": await database.list_traffic_series(range_name)}


@app.get("/api/admin/traffic/settings", dependencies=[Depends(require_admin)])
async def admin_traffic_settings(database: Database = Depends(get_database)) -> dict:
    return _public_traffic_settings(await database.get_traffic_settings())


@app.put("/api/admin/traffic/settings", dependencies=[Depends(require_admin)])
async def update_traffic_settings(
    payload: TrafficSettingsPayload,
    database: Database = Depends(get_database),
) -> dict:
    if payload.monthly_limit_gb > payload.monthly_capacity_gb:
        raise HTTPException(
            status_code=422,
            detail={"code": "TRAFFIC_LIMIT_EXCEEDS_CAPACITY", "message": "允许流量不能超过服务器月容量"},
        )
    try:
        raw = await database.set_traffic_settings(
            enabled=payload.enabled,
            monthly_capacity_bytes=round(payload.monthly_capacity_gb * 1000**3),
            monthly_limit_bytes=round(payload.monthly_limit_gb * 1000**3),
            warning_percent=payload.warning_percent,
            admin_bypass=payload.admin_bypass,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _public_traffic_settings(raw)


@app.post("/api/admin/traffic/reset", dependencies=[Depends(require_admin)])
async def reset_traffic_usage(
    scope: str = Query(default="month", pattern="^(month|all)$"),
    database: Database = Depends(get_database),
) -> dict[str, bool | str]:
    await database.reset_traffic_usage(scope)
    return {"ok": True, "scope": scope}


def _decode_upload_filename(value: str | None, fallback: str) -> str:
    decoded = ""
    if value:
        try:
            decoded = base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            decoded = ""
    decoded = decoded.replace("\\", "/").split("/")[-1]
    decoded = re.sub(r"[\x00-\x1f\x7f]", "", decoded).strip()
    return (decoded or fallback)[:240]


async def _filename_sensitive_matches(database: Database, filename: str) -> list[str]:
    try:
        return await database.sensitive_filename_matches(filename)
    except AttributeError:
        # Compatibility with older test doubles/databases during rolling
        # deployments.  The normal Database implementation always exposes it.
        return []


async def _reject_sensitive_filename(
    database: Database,
    filename: str,
    *,
    actor_key: str | None = None,
    rename: bool = False,
) -> None:
    matches = await _filename_sensitive_matches(database, filename)
    if not matches:
        return
    if rename and actor_key:
        decision = await database.consume_filename_rename_rate(actor_key, matched_word=matches[0])
        if not decision.get("allowed"):
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "FILENAME_RENAME_RATE_LIMITED",
                    "retry_after_seconds": int(decision.get("retry_after_seconds") or 60),
                    "message": "文件名修改尝试过于频繁，请稍后再试",
                },
                headers={"Retry-After": str(int(decision.get("retry_after_seconds") or 60))},
            )
    raise HTTPException(
        status_code=422,
        detail={
            "code": "FILENAME_SENSITIVE_WORD",
            "message": "文件名包含敏感词，无法上传或改名",
            "matches": matches[:5],
        },
    )


async def _bind_invite_settings(database: Database) -> dict[str, Any]:
    def integer(key: str, fallback: int) -> int:
        try:
            return max(1, int(value_cache.get(key, str(fallback))))
        except (TypeError, ValueError):
            return fallback

    value_cache = {
        "enabled": await database.get_setting("bind_invites_enabled", "1"),
        "global": await database.get_setting("bind_invites_global_joins_24h", "100"),
        "per_user": await database.get_setting("bind_invites_per_user_generation_24h", "1"),
    }
    return {
        "enabled": value_cache["enabled"] == "1",
        "global_joins_24h": min(1_000_000, integer("global", 100)),
        "per_user_generation_24h": min(100, integer("per_user", 1)),
    }


async def _integer_setting(database: Database, key: str, fallback: int) -> int:
    try:
        return int(await database.get_setting(key, str(fallback)))
    except (TypeError, ValueError):
        return fallback


async def _release_upload_reservation(telegram: TeleBoxClient, reservation_key: str | None) -> None:
    if reservation_key and hasattr(telegram, "release_upload_quota"):
        try:
            await telegram.release_upload_quota(reservation_key)
        except Exception:
            pass


async def _cancel_upload(
    job: dict[str, Any],
    database: Database,
    telegram: TeleBoxClient,
) -> dict[str, Any]:
    job_id = str(job["id"])
    if str(job.get("status")) in {"completed", "failed", "cancelled"}:
        return _public_upload_job(job)
    cancelled = await database.cancel_upload_job(job_id)
    task = upload_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
    await _release_upload_reservation(telegram, str(job.get("quota_reservation_key") or "") or None)
    if job.get("temp_path"):
        try:
            Path(str(job["temp_path"])).unlink(missing_ok=True)
        except OSError:
            pass
    return _public_upload_job(cancelled or {"id": job_id, "status": "cancelled"})


@app.post("/api/uploads", status_code=202)
async def create_user_upload(
    request: Request,
    visibility: str = Query(default="private", pattern="^(public|private)$"),
    account: str | None = Query(default=None, min_length=1, max_length=40),
    folder_id: int | None = Query(default=None, ge=1),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    indexer: MediaIndexer = Depends(get_indexer),
    traffic: TrafficController = Depends(get_traffic),
    principal: AccessPrincipal = Depends(require_upload_access),
    replication: DisasterRecoveryManager = Depends(get_replication),
) -> dict[str, Any]:
    # The account query remains accepted for old clients, but the WebUI and
    # server always route uploads automatically.  A regular user still cannot
    # smuggle a different binding through a legacy query parameter.
    if account and not principal.is_admin and account != str(principal.account_id or ""):
        raise HTTPException(status_code=403, detail={"code": "ACCOUNT_ACCESS_DENIED"})
    account_id, account_group_id = await automatic_upload_account(principal, telegram, database)
    if folder_id is not None and not await database.get_folder(folder_id):
        raise HTTPException(status_code=404, detail={"code": "FOLDER_NOT_FOUND"})
    try:
        expected_size = int(request.headers.get("content-length") or "0")
    except ValueError as exc:
        raise HTTPException(status_code=411, detail={"code": "CONTENT_LENGTH_REQUIRED"}) from exc
    if expected_size <= 0:
        raise HTTPException(status_code=411, detail={"code": "CONTENT_LENGTH_REQUIRED"})

    job_id = uuid.uuid4().hex
    batch_id = (request.headers.get("x-upload-batch-id") or uuid.uuid4().hex).strip()[:80]
    filename = _decode_upload_filename(request.headers.get("x-upload-filename"), f"upload-{job_id}")
    await _reject_sensitive_filename(database, filename)
    mime_type = (request.headers.get("x-upload-mime") or request.headers.get("content-type") or "application/octet-stream").strip()[:200]
    reservation_key: str | None = None
    if not principal.is_admin:
        if not principal.telegram_user_id:
            raise HTTPException(status_code=403, detail={"code": "TELEGRAM_IDENTITY_REQUIRED"})
        reservation = await telegram.reserve_upload_quota(
            telegram_user_id=principal.telegram_user_id,
            batch_id=batch_id,
            file_count=1,
            total_bytes=expected_size,
        )
        reservation_key = str(reservation.get("reservation_key") or "") or None

    staging_dir = settings.data_dir / "upload-staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        staging_dir.chmod(0o700)
    except OSError:
        pass
    temp_path = staging_dir / f"{job_id}.upload"
    received = 0
    bypass_limit = await _admin_traffic_bypass(principal, database)
    await traffic.start_request("upload", "in")
    try:
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            async for chunk in request.stream():
                if not chunk:
                    continue
                received += len(chunk)
                if received > expected_size:
                    raise HTTPException(status_code=400, detail={"code": "UPLOAD_LENGTH_MISMATCH"})
                await traffic.consume("in", len(chunk), bypass_limit=bypass_limit)
                target.write(chunk)
        if received != expected_size:
            raise HTTPException(status_code=400, detail={"code": "UPLOAD_LENGTH_MISMATCH"})
    except Exception:
        temp_path.unlink(missing_ok=True)
        await _release_upload_reservation(telegram, reservation_key)
        raise
    finally:
        await traffic.finish_request("upload")

    requested_visibility = visibility
    review_status = "approved" if principal.is_admin and visibility == "public" else "pending" if visibility == "public" else "not_required"
    try:
        job = await database.create_upload_job(
            job_id=job_id,
            account_id=account_id,
            filename=filename,
            mime_type=mime_type,
            size=received,
            temp_path=str(temp_path),
            account_group_id=account_group_id,
            owner_user_id=principal.user_id,
            submitter_telegram_user_id=principal.telegram_user_id,
            requested_visibility=requested_visibility,
            review_status=review_status,
            batch_id=batch_id,
            upload_source="web",
            quota_reservation_key=reservation_key,
            folder_id=folder_id,
        )
    except Exception:
        temp_path.unlink(missing_ok=True)
        await _release_upload_reservation(telegram, reservation_key)
        raise
    task = asyncio.create_task(_process_upload_job(job_id, database, telegram, indexer, replication), name=f"upload-{job_id}")
    upload_tasks[job_id] = task
    task.add_done_callback(lambda _: upload_tasks.pop(job_id, None))
    return _public_upload_job(job)


@app.get("/api/uploads")
async def list_my_uploads(
    limit: int = Query(default=100, ge=1, le=500),
    database: Database = Depends(get_database),
    principal: AccessPrincipal = Depends(require_media_access),
) -> dict[str, Any]:
    owner_id = None if principal.is_admin else principal.user_id
    if not principal.is_admin and owner_id is None:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
    return {"items": [_public_upload_job(item) for item in await database.list_upload_jobs(limit, owner_user_id=owner_id)]}


@app.get("/api/uploads/{job_id}")
async def get_my_upload(
    job_id: str,
    database: Database = Depends(get_database),
    principal: AccessPrincipal = Depends(require_media_access),
) -> dict[str, Any]:
    job = await database.get_upload_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "UPLOAD_NOT_FOUND"})
    if not principal.is_admin and (principal.user_id is None or int(job.get("owner_user_id") or -1) != int(principal.user_id)):
        raise HTTPException(status_code=404, detail={"code": "UPLOAD_NOT_FOUND"})
    return _public_upload_job(job)


@app.delete("/api/uploads/{job_id}")
async def cancel_my_upload(
    job_id: str,
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    principal: AccessPrincipal = Depends(require_media_access),
) -> dict[str, Any]:
    job = await database.get_upload_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "UPLOAD_NOT_FOUND"})
    if not principal.is_admin and (principal.user_id is None or int(job.get("owner_user_id") or -1) != int(principal.user_id)):
        raise HTTPException(status_code=404, detail={"code": "UPLOAD_NOT_FOUND"})
    return await _cancel_upload(job, database, telegram)


@app.post("/api/admin/uploads", dependencies=[Depends(require_admin)])
async def create_upload(
    file: UploadFile = File(...),
    account: str | None = Query(default=None, min_length=1, max_length=40),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    indexer: MediaIndexer = Depends(get_indexer),
    traffic: TrafficController = Depends(get_traffic),
    principal: AccessPrincipal = Depends(require_upload_access),
    replication: DisasterRecoveryManager = Depends(get_replication),
) -> dict:
    # Legacy admin endpoint now follows the same automatic account routing as
    # the drag-and-drop WebUI.  `account` is retained only for wire compatibility.
    account_id, account_group_id = await automatic_upload_account(principal, telegram, database)
    staging_dir = settings.data_dir / "upload-staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        staging_dir.chmod(0o700)
    except OSError:
        pass
    job_id = uuid.uuid4().hex
    raw_filename = str(file.filename or "")
    encoded_filename = base64.urlsafe_b64encode(raw_filename.encode("utf-8")).decode("ascii").rstrip("=")
    filename = _decode_upload_filename(encoded_filename, f"upload-{job_id}")
    await _reject_sensitive_filename(database, filename)
    mime_type = (file.content_type or "application/octet-stream").strip()[:200]
    temp_path = staging_dir / f"{job_id}.upload"
    size = 0
    bypass_limit = await _admin_traffic_bypass(principal, database)
    await traffic.start_request("upload", "in")
    try:
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                await traffic.consume("in", len(chunk), bypass_limit=bypass_limit)
                target.write(chunk)
                size += len(chunk)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        await traffic.finish_request("upload")
        await file.close()
    if size <= 0:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail={"code": "EMPTY_UPLOAD"})
    try:
        job = await database.create_upload_job(
            job_id=job_id,
            account_id=account_id,
            filename=filename,
            mime_type=mime_type,
            size=size,
            temp_path=str(temp_path),
            account_group_id=account_group_id,
            owner_user_id=principal.user_id,
            submitter_telegram_user_id=principal.telegram_user_id,
            upload_source="web_admin_compat",
        )
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    task = asyncio.create_task(_process_upload_job(job_id, database, telegram, indexer, replication), name=f"upload-{job_id}")
    upload_tasks[job_id] = task
    task.add_done_callback(lambda _: upload_tasks.pop(job_id, None))
    return _public_upload_job(job)


@app.get("/api/admin/uploads/{job_id}", dependencies=[Depends(require_admin)])
async def get_upload(job_id: str, database: Database = Depends(get_database)) -> dict:
    job = await database.get_upload_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "UPLOAD_NOT_FOUND"})
    return _public_upload_job(job)


@app.delete("/api/admin/uploads/{job_id}", dependencies=[Depends(require_admin)])
async def cancel_upload(
    job_id: str,
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict:
    job = await database.get_upload_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "UPLOAD_NOT_FOUND"})
    return await _cancel_upload(job, database, telegram)


@app.put("/api/admin/helper-bot", dependencies=[Depends(require_admin)])
async def update_helper_bot(
    payload: HelperBotPayload,
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict:
    await telegram.set_helper_bot(payload.token.strip())
    return await telegram.helper_bot_status()


@app.get("/api/admin/helper-bot/rate-limit", dependencies=[Depends(require_admin)])
async def get_helper_bot_rate_limit(
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict[str, Any]:
    try:
        return await telegram.helper_bot_rate_limit()
    except (AttributeError, TelegramUnavailable):
        return default_helper_rate_limit()


@app.put("/api/admin/helper-bot/rate-limit", dependencies=[Depends(require_admin)])
async def update_helper_bot_rate_limit(
    payload: HelperRateLimitPayload,
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict[str, Any]:
    if payload.max_file_bytes > payload.max_album_bytes:
        raise HTTPException(
            status_code=422,
            detail={"code": "HELPER_FILE_LIMIT_EXCEEDS_ALBUM_LIMIT"},
        )
    try:
        return await telegram.set_helper_bot_rate_limit(payload.model_dump())
    except AttributeError as exc:
        raise HTTPException(status_code=503, detail={"code": "HELPER_RATE_LIMIT_UNAVAILABLE"}) from exc


@app.post("/api/admin/accounts/{account_id}/invites", dependencies=[Depends(require_admin)])
async def create_account_invite(
    account_id: str,
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict:
    return await telegram.create_invite(account_id)


@app.post("/api/admin/accounts", dependencies=[Depends(require_admin)])
async def create_managed_account(
    payload: AccountPayload,
    telegram: TeleBoxClient = Depends(get_telegram),
    database: Database = Depends(get_database),
) -> dict:
    group_id = str(payload.group_id or f"account-{payload.id}")
    if payload.role == "replica" and not await database.get_account_group(group_id):
        raise HTTPException(status_code=422, detail={"code": "ACCOUNT_GROUP_NOT_FOUND"})
    if payload.role == "primary" and payload.group_id and await database.get_account_group(group_id):
        raise HTTPException(status_code=409, detail={"code": "PRIMARY_ACCOUNT_ALREADY_EXISTS"})
    account_payload = payload.model_dump()
    account_payload.pop("group_id", None)
    account_payload.pop("role", None)
    account_payload.pop("priority", None)
    created = await telegram.create_account(account_payload)
    if payload.role == "replica":
        await database.add_account_group_member(group_id, payload.id, role="replica", priority=payload.priority)
    else:
        await database.ensure_account_group(group_id, name=payload.label, primary_account_id=payload.id)
    return {**created, "group_id": group_id, "role": payload.role, "priority": payload.priority}


@app.post("/api/admin/accounts/{account_id}/login/qr", dependencies=[Depends(require_admin)])
async def start_account_qr_login(
    account_id: str,
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict:
    return await telegram.start_account_qr_login(account_id)


@app.get("/api/admin/accounts/{account_id}/login", dependencies=[Depends(require_admin)])
async def account_login_status(
    account_id: str,
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict:
    return await telegram.account_login_status(account_id)


@app.delete("/api/admin/accounts/{account_id}/login", dependencies=[Depends(require_admin)])
async def cancel_account_login(
    account_id: str,
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict:
    return await telegram.cancel_account_login(account_id)


@app.get("/api/admin/account-groups", dependencies=[Depends(require_admin)])
async def list_admin_account_groups(
    database: Database = Depends(get_database),
    replication: DisasterRecoveryManager = Depends(get_replication),
) -> dict[str, Any]:
    await replication.ensure_groups()
    return {"items": await database.list_account_groups()}


@app.get("/api/admin/account-groups/{group_id}", dependencies=[Depends(require_admin)])
async def get_admin_account_group(group_id: str, database: Database = Depends(get_database)) -> dict[str, Any]:
    group = await database.get_account_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_GROUP_NOT_FOUND"})
    return group


@app.put("/api/admin/account-groups/{group_id}/settings", dependencies=[Depends(require_admin)])
async def update_admin_account_group_settings(
    group_id: str,
    payload: AccountGroupSettingsPayload,
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    group = await database.update_account_group(
        group_id,
        auto_failover_enabled=1 if payload.auto_failover_enabled else 0,
        replication_enabled=1 if payload.replication_enabled else 0,
        rate_min_interval_ms=payload.rate_min_interval_ms,
        rate_max_messages_per_minute=payload.rate_max_messages_per_minute,
        rate_concurrency=payload.rate_concurrency,
    )
    if not group:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_GROUP_NOT_FOUND"})
    return group


@app.post("/api/admin/account-groups/{group_id}/replicas/{account_id}/sync", dependencies=[Depends(require_admin)])
async def start_admin_replica_sync(
    group_id: str,
    account_id: str,
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    group = await database.get_account_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_GROUP_NOT_FOUND"})
    member = next((item for item in group.get("members", []) if str(item.get("account_id")) == account_id), None)
    if not member or str(member.get("role")) != "replica":
        raise HTTPException(status_code=422, detail={"code": "REPLICA_NOT_FOUND"})
    await database.update_account_group_member(group_id, account_id, sync_status="pending", sync_cursor=None, processed_files=0, processed_bytes=0, total_files=0, total_bytes=0, last_error=None)
    return {"ok": True, "group_id": group_id, "account_id": account_id, "status": "pending"}


@app.get("/api/admin/replication/jobs/{job_id}", dependencies=[Depends(require_admin)])
async def get_admin_replication_job(job_id: str, database: Database = Depends(get_database)) -> dict[str, Any]:
    job = await database.get_replication_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "REPLICATION_JOB_NOT_FOUND"})
    return job


@app.post("/api/admin/replication/jobs/{job_id}/retry", dependencies=[Depends(require_admin)])
async def retry_admin_replication_job(job_id: str, database: Database = Depends(get_database)) -> dict[str, Any]:
    job = await database.get_replication_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "REPLICATION_JOB_NOT_FOUND"})
    updated = await database.update_replication_job(job_id, status="queued", phase="queued", error=None, next_retry_at=None)
    if str(job.get("job_type")) == "backfill" and job.get("group_id") and job.get("target_account_id"):
        await database.update_account_group_member(
            str(job["group_id"]),
            str(job["target_account_id"]),
            sync_status="running",
            last_error=None,
        )
    return updated or job


@app.post("/api/admin/account-groups/{group_id}/failover", dependencies=[Depends(require_admin)])
async def manual_admin_failover(
    group_id: str,
    payload: ManualFailoverPayload,
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict[str, Any]:
    group = await database.get_account_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_GROUP_NOT_FOUND"})
    target = next((item for item in group.get("members", []) if str(item.get("account_id")) == payload.target_account_id), None)
    if not target or not int(target.get("enabled") or 0) or str(target.get("role")) != "replica" or str(target.get("sync_status")) != "ready":
        raise HTTPException(status_code=422, detail={"code": "REPLICA_NOT_READY"})
    account_rows = (await telegram.accounts()).get("items", [])
    state = next((item.get("state") for item in account_rows if str(item.get("id")) == payload.target_account_id), None)
    if state != "authenticated":
        raise HTTPException(status_code=409, detail={"code": "REPLICA_NOT_AUTHENTICATED"})
    previous = str(group.get("active_account_id") or "")
    await database.record_failover(group_id, previous, payload.target_account_id, "manual administrator failover", int(group.get("health_failures") or 0))
    return await database.get_account_group(group_id)  # type: ignore[return-value]


@app.delete("/api/admin/bindings", dependencies=[Depends(require_admin)])
async def remove_binding(
    payload: BindingPayload,
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict:
    return await telegram.delete_binding(payload.submitter_id)


@app.put("/api/admin/users/{user_id}")
async def update_auth_user(
    user_id: int,
    payload: AdminUserUpdatePayload,
    principal: AccessPrincipal = Depends(require_admin),
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict[str, Any]:
    current = await auth.get_user(user_id)
    if not current:
        raise HTTPException(status_code=404, detail={"code": "AUTH_USER_NOT_FOUND"})
    if payload.role == "superadmin" and principal.role != "superadmin":
        raise HTTPException(status_code=403, detail={"code": "SUPERADMIN_REQUIRED"})
    removes_last_superadmin = (
        str(current.get("role")) == "superadmin"
        and str(current.get("status")) == "approved"
        and (payload.role not in {None, "superadmin"} or payload.status in {"pending", "disabled", "denied"})
    )
    if removes_last_superadmin and await auth.superadmin_count() <= 1:
        raise HTTPException(status_code=409, detail={"code": "LAST_SUPERADMIN_REQUIRED"})

    account_id = payload.account_id.strip() if payload.account_id is not None else None
    if account_id:
        accounts = (await telegram.accounts()).get("items", [])
        if not any(str(item.get("id")) == account_id for item in accounts):
            raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    updated = await auth.update_user(
        user_id,
        status=payload.status,
        role=payload.role,
        account_id=account_id,
        ban_reason=payload.ban_reason,
    )
    if not updated:
        raise HTTPException(status_code=404, detail={"code": "AUTH_USER_NOT_FOUND"})

    telegram_user_id = str(updated.get("telegram_user_id") or "")
    if telegram_user_id and payload.status is not None:
        binding_updater = getattr(telegram, "set_binding_status", None)
        if binding_updater is not None:
            await binding_updater(
                telegram_user_id,
                enabled=payload.status == "approved",
                banned=payload.status == "disabled",
                reason=payload.ban_reason or ("管理员禁用账号" if payload.status == "disabled" else None),
            )
    if payload.status == "approved" or account_id:
        updated = await _sync_auth_user_binding(
            auth,
            telegram,
            updated,
            requires_approval=(await _registration_config(database))[4],
        ) or updated
    await auth.audit(user_id, "admin_user_updated")
    return _safe_user(updated) or {}


@app.put("/api/admin/access-users/{telegram_user_id}", dependencies=[Depends(require_admin)])
async def update_access_user(
    telegram_user_id: str,
    payload: AccessUserStatusPayload,
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict:
    user = await database.set_media_user_status(telegram_user_id, payload.status)
    if not user:
        raise HTTPException(status_code=404, detail={"code": "ACCESS_USER_NOT_FOUND"})
    binding_updater = getattr(telegram, "set_binding_status", None)
    if binding_updater is not None:
        await binding_updater(
            telegram_user_id,
            enabled=payload.status != "disabled",
            banned=payload.status == "disabled",
            reason="管理员在访问用户面板中更新状态" if payload.status == "disabled" else None,
        )
    elif payload.status == "disabled":
        deleter = getattr(telegram, "delete_binding", None)
        if deleter is not None:
            await deleter(telegram_user_id)
    return user


@app.post("/api/admin/ingest/jobs/{job_id}/retry", dependencies=[Depends(require_admin)])
async def retry_ingest_job(
    job_id: int,
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict:
    return await telegram.retry_job(job_id)


@app.put("/api/admin/settings", dependencies=[Depends(require_admin)])
async def update_admin_settings(
    payload: SettingsPayload,
    database: Database = Depends(get_database),
    cache: DiskCache = Depends(get_cache),
) -> dict[str, bool]:
    await database.update_access_settings(
        cache_max_bytes=int(payload.cache_max_gb * 1024**3),
        access_restricted=True,
        viewer_key_hash="",
    )
    await cache.evict_if_needed()
    return {"ok": True}


@app.delete("/api/admin/cache", dependencies=[Depends(require_admin)])
async def clear_cache(cache: DiskCache = Depends(get_cache)) -> dict[str, bool]:
    await cache.clear()
    return {"ok": True}


@app.put("/api/admin/media/{message_id}", dependencies=[Depends(require_admin)])
async def update_media_title(
    message_id: int,
    payload: TitlePayload,
    account: str | None = Query(default=None, min_length=1, max_length=40),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    replication: DisasterRecoveryManager = Depends(get_replication),
    principal: AccessPrincipal = Depends(require_admin),
) -> dict[str, bool]:
    account = await telegram.resolve_account(account)
    if not await database.get_media_index(account, message_id):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_INDEX_NOT_FOUND"})
    await _reject_sensitive_filename(
        database,
        payload.title,
        actor_key=f"user:{principal.user_id or 'recovery'}",
        rename=True,
    )
    await database.set_local_title(message_id, payload.title, account)
    await _queue_replication_mutation(replication, account, message_id, "caption", caption=payload.title)
    return {"ok": True}


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        requested = STATIC_DIR / path
        if path and requested.is_file() and STATIC_DIR in requested.resolve().parents:
            return FileResponse(requested)
        return FileResponse(STATIC_DIR / "index.html")

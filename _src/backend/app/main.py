from __future__ import annotations

import asyncio
import hashlib
import secrets
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Cookie, Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile, status
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
from .media_crypto import DeviceKeyError, encrypt_for_device, load_device_public_key, parse_device_public_key
from .ranges import InvalidRange, parse_range_header
from .security import TokenSigner, constant_time_equal
from .storage import storage_snapshot, storage_watchdog
from .traffic import TrafficController, TrafficLimitExceeded
from .telebox_client import (
    TELEGRAM_CHUNK_SIZE,
    MediaNotFound,
    InvalidWebLoginCode,
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


class AdminCreatePayload(UserPasswordPayload):
    role: str = Field(default="admin", pattern="^(admin|superadmin)$")


class PasswordResetCompletePayload(BaseModel):
    challenge_id: str = Field(min_length=20, max_length=256)
    password: str = Field(min_length=1, max_length=128)


class PublicAlbumSettingsPayload(BaseModel):
    enabled: bool | None = None
    registration_enabled: bool | None = None
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
    app.state.database = database
    app.state.auth = auth
    app.state.cache = cache
    app.state.traffic = traffic
    app.state.telegram = telegram
    app.state.indexer = indexer
    await indexer.start()
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
    await indexer.stop()
    pending_uploads = list(upload_tasks.values())
    for task in pending_uploads:
        if not task.done():
            task.cancel()
    if pending_uploads:
        await asyncio.gather(*pending_uploads, return_exceptions=True)
    upload_tasks.clear()
    await telegram.close()


app = FastAPI(title="SavedStream", version="0.1.0", lifespan=lifespan)

# Keep a process-local handle for administrator cancellation.  The upload
# state itself remains durable in SQLite, so a restart still exposes the last
# known state and the normal retry path can be used afterwards.
upload_tasks: dict[str, asyncio.Task[None]] = {}


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


async def _registration_config(database: Database) -> tuple[bool, str, int, str]:
    enabled = await database.get_setting("public_registration_enabled", "0") == "1"
    key_hash = await database.get_setting("registration_key_hash", "")
    try:
        version = int(await database.get_setting("registration_key_version", "1"))
    except ValueError:
        version = 1
    fingerprint = await database.get_setting("registration_key_fingerprint", "")
    return enabled, key_hash, version, fingerprint


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
        "created_at": user.get("created_at"),
        "approved_at": user.get("approved_at"),
    }


async def optional_access_principal(
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
    admin_cookie: str | None = Cookie(default=None, alias=ADMIN_COOKIE),
    auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE),
) -> AccessPrincipal | None:
    if signer.verify(admin_cookie, "admin", "control"):
        return AccessPrincipal(is_admin=True, role="superadmin", user_status="approved", binding_sync_status="ready")
    user = await auth.get_session(auth_cookie)
    if not user:
        return None
    is_admin = str(user.get("role")) in {"admin", "superadmin"} and str(user.get("status")) == "approved"
    return AccessPrincipal(
        is_admin=is_admin,
        user_id=int(user["id"]),
        username=str(user.get("username_display") or user.get("username_normalized") or "") or None,
        role=str(user.get("role") or "user"),
        telegram_user_id=str(user["telegram_user_id"]),
        account_id=str(user["account_id"]) if user.get("account_id") else None,
        user_status=str(user["status"]),
        binding_sync_status=str(user.get("binding_sync_status") or "pending"),
        public_authenticated=True,
    )


async def require_admin(
    principal: AccessPrincipal | None = Depends(optional_access_principal),
) -> AccessPrincipal:
    if principal and principal.is_admin:
        return principal
    raise HTTPException(status_code=401, detail={"code": "ADMIN_AUTH_REQUIRED"})


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


async def authorized_account(
    requested_account: str | None,
    principal: AccessPrincipal,
    telegram: TeleBoxClient,
) -> str:
    if not principal.is_admin:
        requested = (requested_account or principal.account_id or "").strip()
        if requested in {"", "all", "*"}:
            requested = principal.account_id or ""
        if not requested:
            raise HTTPException(status_code=403, detail={"code": "ACCOUNT_ACCESS_DENIED"})
        accounts = (await telegram.accounts()).get("items", [])
        if not any(str(item.get("id")) == requested for item in accounts):
            raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
        return requested
    return await telegram.resolve_account(requested_account or settings.telebox_default_account)


async def account_filter(
    requested_account: str | None,
    principal: AccessPrincipal,
    telegram: TeleBoxClient,
) -> str | None:
    """Resolve a list filter while allowing public media across accounts."""
    requested = (requested_account or "").strip()
    if not requested or requested in {"all", "*"}:
        return None
    accounts = (await telegram.accounts()).get("items", [])
    if not any(str(item.get("id")) == requested for item in accounts):
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    return requested


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
    ) and not (
        item.get("submitter_telegram_user_id")
        and str(item.get("submitter_telegram_user_id")) == str(principal.telegram_user_id)
        and not item.get("hidden")
    ):
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


@app.get("/api/status")
async def public_status(
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    admin_cookie: str | None = Cookie(default=None, alias=ADMIN_COOKIE),
    principal: AccessPrincipal | None = Depends(optional_access_principal),
) -> dict:
    tg_status = await telegram.status()
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
        "media_session_id": media_session_id,
        "registration_enabled": (await _registration_config(database))[0],
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
    database: Database = Depends(get_database),
    auth: AuthStore = Depends(get_auth),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict[str, Any]:
    enabled, key_hash, version, _ = await _registration_config(database)
    if not enabled:
        raise HTTPException(status_code=403, detail={"code": "REGISTRATION_DISABLED"})
    if not key_hash or not _verify_public_key(payload.registration_key, key_hash):
        await asyncio.sleep(0.2)
        raise HTTPException(status_code=401, detail={"code": "INVALID_REGISTRATION_KEY"})
    try:
        token, challenge = await auth.register_challenge(payload.username, payload.password)
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
) -> dict[str, Any]:
    challenge = await auth.get_challenge(challenge_id, kind="device_verify")
    if not challenge:
        raise HTTPException(status_code=404, detail={"code": "AUTH_CHALLENGE_NOT_FOUND"})
    if challenge["status"] == "claimed":
        user = await auth.get_user(int(challenge["user_id"]))
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
    auth: AuthStore = Depends(get_auth),
) -> dict[str, Any]:
    expected = settings.savedstream_internal_token or settings.telebox_api_token
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not expected or not supplied or not constant_time_equal(supplied, expected):
        raise HTTPException(status_code=401, detail={"code": "INTERNAL_AUTH_REQUIRED"})
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
    return {"ok": True, "challenge_id": challenge.get("id"), "status": challenge.get("status"), "user_id": challenge.get("user_id")}


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
    kind: str = Query(default="all", pattern="^(all|video|image|audio|file)$"),
    q: str = Query(default="", max_length=100),
    account: str | None = Query(default=None, min_length=1, max_length=40),
    scope: str = Query(default="public", pattern="^(public|private|hidden|all)$"),
    folder: int | None = Query(default=None, alias="folder_id", ge=0),
    date_from: str | None = Query(default=None, alias="from", max_length=10),
    date_to: str | None = Query(default=None, alias="to", max_length=10),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    principal: AccessPrincipal = Depends(require_media_access),
) -> dict:
    account_id = await account_filter(account, principal, telegram)
    visibility = scope if principal.is_admin else "all"
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
        owner_telegram_user_id=None if principal.is_admin else principal.telegram_user_id,
        folder_id=folder,
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
        "index": await database.get_sync_state(account),
    }


@app.get("/api/media/timeline", dependencies=[Depends(require_viewer)])
async def media_timeline(
    account: str | None = Query(default=None, min_length=1, max_length=40),
    kind: str = Query(default="all", pattern="^(all|video|image|audio|file)$"),
    q: str = Query(default="", max_length=100),
    scope: str = Query(default="public", pattern="^(public|private|hidden|all)$"),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    principal: AccessPrincipal = Depends(require_media_access),
) -> dict:
    account_id = await account_filter(account, principal, telegram)
    visibility = scope if principal.is_admin else "all"
    return {
        "account_id": account_id,
        "scope": visibility,
        "years": await database.list_timeline(
            account_id=account_id,
            visibility=visibility,
            kind=kind,
            query=q.strip(),
            owner_telegram_user_id=None if principal.is_admin else principal.telegram_user_id,
        ),
        "index": await database.get_sync_state(account_id) if account_id else None,
    }


@app.get("/api/accounts", dependencies=[Depends(require_viewer)])
async def public_accounts(
    telegram: TeleBoxClient = Depends(get_telegram),
    principal: AccessPrincipal = Depends(require_media_access),
) -> dict:
    payload = await telegram.accounts()
    items = payload.get("items", [])
    configured_default = settings.telebox_default_account
    selected_default = configured_default if any(item.get("id") == configured_default for item in items) else next((item["id"] for item in items if item.get("state") == "authenticated"), items[0]["id"] if items else configured_default)
    return {
        "items": [
            {"id": item["id"], "label": item.get("label", item["id"]), "state": item.get("state", "unknown")}
            for item in items
        ],
        "default_account": selected_default,
    }


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
    account = await authorized_account(account, principal, telegram)
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
    account = await authorized_account(account, principal, telegram)
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
    account = await authorized_account(account, principal, telegram)
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
    account = await authorized_account(account, principal, telegram)
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
) -> dict:
    cache_limit = await database.get_cache_limit()
    cache_stats = await cache.stats()
    viewer_hash = await database.get_setting("viewer_key_hash", "")
    public_enabled, public_key_hash, public_key_version = await _public_album_config(database)
    registration_enabled, registration_hash, registration_version, registration_fingerprint = await _registration_config(database)
    try:
        helper_rate_limit = await telegram.helper_bot_rate_limit()
    except (AttributeError, TelegramUnavailable):
        helper_rate_limit = default_helper_rate_limit()
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
        "telegram": await telegram.status(),
        "accounts": (await telegram.accounts()).get("items", []),
        "helper_bot": await telegram.helper_bot_status(),
        "bindings": (await telegram.bindings()).get("items", []),
        "ingest_jobs": (await telegram.jobs()).get("items", []),
        "access_users": await database.list_media_users(),
        "auth_users": [_safe_user(user) | {"password_hash": None} for user in await auth.list_users()],
        "media_sync": await database.list_sync_states(),
        "upload_jobs": [_public_upload_job(item) for item in await database.list_upload_jobs()],
        "traffic": await _traffic_summary(database, traffic),
        "helper_rate_limit": helper_rate_limit,
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
async def admin_public_album(database: Database = Depends(get_database)) -> dict[str, bool | int]:
    enabled, key_hash, version = await _public_album_config(database)
    registration_enabled, registration_hash, registration_version, fingerprint = await _registration_config(database)
    return {
        "enabled": enabled,
        "key_configured": bool(key_hash),
        "key_version": version,
        "registration_enabled": registration_enabled,
        "registration_key_configured": bool(registration_hash),
        "registration_key_version": registration_version,
        "registration_key_fingerprint": fingerprint,
    }


@app.put("/api/admin/public-album", dependencies=[Depends(require_admin)])
async def update_public_album(
    payload: PublicAlbumSettingsPayload,
    database: Database = Depends(get_database),
) -> dict[str, bool | int]:
    enabled, album_hash, album_version = await _public_album_config(database)
    registration_enabled, registration_hash, registration_version, fingerprint = await _registration_config(database)
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
    return {
        "enabled": enabled,
        "key_configured": bool(album_hash),
        "key_version": album_version,
        "registration_enabled": registration_enabled,
        "registration_key_configured": bool(registration_hash or payload.registration_key),
        "registration_key_version": registration_version,
        "registration_key_fingerprint": fingerprint,
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
    raw_key = payload.key.strip() if payload.key else secrets.token_urlsafe(32)
    _, _, current_version, _ = await _registration_config(database)
    version = current_version + 1
    fingerprint = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
    await database.set_setting("registration_key_hash", _hash_public_key(raw_key))
    await database.set_setting("registration_key_version", str(version))
    await database.set_setting("registration_key_fingerprint", fingerprint)
    await database.set_setting("public_registration_enabled", "0")
    return {"key": raw_key, "key_version": version, "fingerprint": fingerprint, "enabled": False}


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


@app.post("/api/admin/media/{message_id}/review", dependencies=[Depends(require_admin)])
async def review_media(
    message_id: int,
    payload: ReviewPayload,
    account: str | None = Query(default=None, min_length=1, max_length=40),
    database: Database = Depends(get_database),
    cache: DiskCache = Depends(get_cache),
    telegram: TeleBoxClient = Depends(get_telegram),
    indexer: MediaIndexer = Depends(get_indexer),
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
        await database.upsert_media_index(item, visibility="private")
        await database.rebuild_timeline(str(job["account_id"]))
        await database.complete_upload_job(job_id, message_id=int(item["id"]))
        # Make the item visible to the next local query immediately, while the
        # normal background worker will reconcile it on its next pass.
        indexer.schedule_sync(str(job["account_id"]), full=False)
    except asyncio.CancelledError:
        await database.update_upload_job(job_id, status="cancelled", phase="cancelled", error="upload cancelled")
        raise
    except Exception as exc:
        await database.update_upload_job(job_id, status="failed", phase="failed", error=str(exc), temp_path=None)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _public_upload_job(job: dict | None) -> dict:
    if not job:
        return {}
    return {key: value for key, value in job.items() if key != "temp_path"}


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


@app.post("/api/admin/uploads", dependencies=[Depends(require_admin)])
async def create_upload(
    file: UploadFile = File(...),
    account: str | None = Query(default=None, min_length=1, max_length=40),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    indexer: MediaIndexer = Depends(get_indexer),
    traffic: TrafficController = Depends(get_traffic),
) -> dict:
    account_id = await telegram.resolve_account(account)
    staging_dir = settings.data_dir / "upload-staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    filename = (file.filename or f"upload-{job_id}").strip()[:240]
    mime_type = (file.content_type or "application/octet-stream").strip()[:200]
    temp_path = staging_dir / f"{job_id}.upload"
    size = 0
    bypass_limit = await _admin_traffic_bypass(AccessPrincipal(is_admin=True), database)
    await traffic.start_request("upload", "in")
    try:
        with temp_path.open("wb") as target:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                await traffic.consume("in", len(chunk), bypass_limit=bypass_limit)
                target.write(chunk)
                size += len(chunk)
    except TrafficLimitExceeded:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        await traffic.finish_request("upload")
        await file.close()
    if size <= 0:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail={"code": "EMPTY_UPLOAD"})
    job = await database.create_upload_job(
        job_id=job_id,
        account_id=account_id,
        filename=filename,
        mime_type=mime_type,
        size=size,
        temp_path=str(temp_path),
    )
    task = asyncio.create_task(_process_upload_job(job_id, database, telegram, indexer), name=f"upload-{job_id}")
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
async def cancel_upload(job_id: str, database: Database = Depends(get_database)) -> dict:
    job = await database.get_upload_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "UPLOAD_NOT_FOUND"})
    if str(job.get("status")) in {"completed", "failed", "cancelled"}:
        return _public_upload_job(job)
    cancelled = await database.cancel_upload_job(job_id)
    task = upload_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
    if job.get("temp_path"):
        try:
            Path(str(job["temp_path"])).unlink(missing_ok=True)
        except OSError:
            pass
    return _public_upload_job(cancelled or {"id": job_id, "status": "cancelled"})


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
) -> dict:
    return await telegram.create_account(payload.model_dump())


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


@app.delete("/api/admin/bindings", dependencies=[Depends(require_admin)])
async def remove_binding(
    payload: BindingPayload,
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict:
    return await telegram.delete_binding(payload.submitter_id)


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
) -> dict[str, bool]:
    account = await telegram.resolve_account(account)
    if not await database.get_media_index(account, message_id):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_INDEX_NOT_FOUND"})
    await database.set_local_title(message_id, payload.title, account)
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

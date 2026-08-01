from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .cache import DiskCache
from .config import settings
from .database import Database
from .media_crypto import DeviceKeyError, encrypt_for_device, load_device_public_key, parse_device_public_key
from .ranges import InvalidRange, parse_range_header
from .security import TokenSigner, constant_time_equal
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
DEVICE_COOKIE = "savedstream_device"
COOKIE_TTL = settings.session_cookie_days * 24 * 60 * 60
signer = TokenSigner(f"{settings.admin_key}:{settings.api_hash}:savedstream")


class KeyPayload(BaseModel):
    key: str = Field(min_length=1, max_length=512)


class DeviceKeyPayload(BaseModel):
    device_public_key: str = Field(min_length=300, max_length=4096)
    key_format: str = Field(pattern="^spki-rsa-oaep-v1$")


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


@dataclass(frozen=True)
class AccessPrincipal:
    is_admin: bool
    telegram_user_id: str | None = None
    account_id: str | None = None
    user_status: str = "approved"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    database = Database(settings.database_path)
    await database.initialize()
    cache = DiskCache(settings.cache_dir, database.get_cache_limit, settings.media_cache_key)
    await cache.initialize()
    telegram = TeleBoxClient(settings)
    await telegram.initialize()
    app.state.database = database
    app.state.cache = cache
    app.state.telegram = telegram
    yield
    await telegram.close()


app = FastAPI(title="SavedStream", version="0.1.0", lifespan=lifespan)


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_cache(request: Request) -> DiskCache:
    return request.app.state.cache


def get_telegram(request: Request) -> TeleBoxClient:
    return request.app.state.telegram


async def require_admin(
    admin_cookie: str | None = Cookie(default=None, alias=ADMIN_COOKIE),
) -> None:
    if not signer.verify(admin_cookie, "admin", "control"):
        raise HTTPException(status_code=401, detail={"code": "ADMIN_AUTH_REQUIRED"})


async def optional_access_principal(
    database: Database = Depends(get_database),
    admin_cookie: str | None = Cookie(default=None, alias=ADMIN_COOKIE),
    access_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
) -> AccessPrincipal | None:
    if signer.verify(admin_cookie, "admin", "control"):
        return AccessPrincipal(is_admin=True)
    user = await database.get_access_session(access_cookie)
    if not user:
        return None
    return AccessPrincipal(
        is_admin=False,
        telegram_user_id=str(user["telegram_user_id"]),
        account_id=str(user["account_id"]),
        user_status=str(user["status"]),
    )


async def require_media_access(
    principal: AccessPrincipal | None = Depends(optional_access_principal),
) -> AccessPrincipal:
    if principal and (principal.is_admin or principal.user_status == "approved"):
        return principal
    if principal:
        raise HTTPException(status_code=403, detail={"code": f"ACCESS_{principal.user_status.upper()}"})
    raise HTTPException(status_code=401, detail={"code": "MEDIA_AUTH_REQUIRED"})


require_viewer = require_media_access


async def authorized_account(
    requested_account: str | None,
    principal: AccessPrincipal,
    telegram: TeleBoxClient,
) -> str:
    if not principal.is_admin:
        if requested_account and requested_account != principal.account_id:
            raise HTTPException(status_code=403, detail={"code": "ACCOUNT_ACCESS_DENIED"})
        assert principal.account_id is not None
        resolved = await telegram.resolve_account(principal.account_id)
        if resolved != principal.account_id:
            raise HTTPException(status_code=403, detail={"code": "ACCOUNT_ACCESS_DENIED"})
        return resolved
    return await telegram.resolve_account(requested_account or settings.telebox_default_account)


@app.exception_handler(TelegramUnavailable)
async def telegram_unavailable_handler(_: Request, exc: TelegramUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc), "code": "TELEGRAM_UNAVAILABLE"})


@app.exception_handler(MediaNotFound)
async def media_not_found_handler(_: Request, exc: MediaNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc), "code": "MEDIA_NOT_FOUND"})


@app.get("/api/status")
async def public_status(
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    admin_cookie: str | None = Cookie(default=None, alias=ADMIN_COOKIE),
    principal: AccessPrincipal | None = Depends(optional_access_principal),
) -> dict:
    tg_status = await telegram.status()
    admin_authenticated = signer.verify(admin_cookie, "admin", "control")
    media_authenticated = bool(principal and (principal.is_admin or principal.user_status == "approved"))
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
    }


@app.post("/api/admin/login")
async def admin_login(payload: KeyPayload, response: Response) -> dict[str, bool]:
    if not settings.admin_key or not constant_time_equal(payload.key, settings.admin_key):
        await asyncio.sleep(0.35)
        raise HTTPException(status_code=401, detail={"code": "INVALID_ADMIN_KEY"})
    token = signer.issue("admin", "control", COOKIE_TTL)
    response.set_cookie(
        ADMIN_COOKIE,
        token,
        max_age=COOKIE_TTL,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return {"ok": True}


@app.post("/api/admin/logout")
async def admin_logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(ADMIN_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/access/login")
async def viewer_login(
) -> dict[str, bool]:
    raise HTTPException(status_code=410, detail={"code": "VIEWER_PASSWORD_REMOVED"})


@app.post("/api/access/telegram")
async def telegram_access_login(
    payload: TelegramAccessPayload,
    response: Response,
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict:
    try:
        identity = await telegram.consume_web_login_code(payload.code.strip())
    except InvalidWebLoginCode as exc:
        await asyncio.sleep(0.35)
        raise HTTPException(status_code=401, detail={"code": "INVALID_TELEGRAM_LOGIN_CODE"}) from exc
    user = await database.upsert_media_user(identity)
    token = secrets.token_urlsafe(32)
    await database.create_access_session(token, str(user["telegram_user_id"]), COOKIE_TTL)
    response.set_cookie(
        ACCESS_COOKIE,
        token,
        max_age=COOKIE_TTL,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    response.delete_cookie(VIEWER_COOKIE, path="/")
    return {"ok": True, "status": user["status"], "user": user}


@app.get("/api/access/telegram/status")
async def telegram_access_status(
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    access_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
) -> dict:
    user = await database.get_access_session(access_cookie)
    try:
        helper_bot_username = (await telegram.helper_bot_status()).get("username")
    except TelegramUnavailable:
        helper_bot_username = None
    return {
        "authenticated": bool(user),
        "status": user["status"] if user else "unauthenticated",
        "user": user,
        "helper_bot_username": helper_bot_username,
    }


@app.post("/api/access/telegram/logout")
async def telegram_access_logout(
    response: Response,
    database: Database = Depends(get_database),
    access_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
) -> dict[str, bool]:
    await database.revoke_access_session(access_cookie)
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(VIEWER_COOKIE, path="/")
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
        max_age=COOKIE_TTL,
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
    cursor: int | None = Query(default=None, ge=1),
    order: str = Query(default="newest", pattern="^(newest|oldest)$"),
    kind: str = Query(default="all", pattern="^(all|video|image|audio|file)$"),
    q: str = Query(default="", max_length=100),
    account: str | None = Query(default=None, min_length=1, max_length=40),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    principal: AccessPrincipal = Depends(require_media_access),
) -> dict:
    account = await authorized_account(account, principal, telegram)
    items, next_cursor, has_more = await telegram.list_saved_media(
        account_id=account,
        limit=limit,
        cursor=cursor,
        order=order,
        kind=kind,
        query=q.strip(),
    )
    local_titles = await database.get_local_titles([item["id"] for item in items], account)
    for item in items:
        item["local_title"] = local_titles.get(item["id"])
        item["title"] = item["local_title"] or item["original_title"]
        item["account_id"] = account
        item["thumbnail_url"] = f"/api/media/{item['id']}/thumbnail?account={quote(account)}&size={item['size']}&v=2" if item["has_thumbnail"] else None
        item["stream_url"] = f"/api/media/{item['id']}/stream?account={quote(account)}"
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


@app.get("/api/accounts", dependencies=[Depends(require_viewer)])
async def public_accounts(
    telegram: TeleBoxClient = Depends(get_telegram),
    principal: AccessPrincipal = Depends(require_media_access),
) -> dict:
    payload = await telegram.accounts()
    items = payload.get("items", [])
    if not principal.is_admin:
        items = [item for item in items if item.get("id") == principal.account_id]
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
    telegram: TeleBoxClient = Depends(get_telegram),
    principal: AccessPrincipal = Depends(require_media_access),
) -> Response:
    account = await authorized_account(account, principal, telegram)
    if size is not None:
        cache_key = telegram.media_cache_key(
            {"account_id": account, "id": message_id}, {"size": size}
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
    return Response(
        content=data,
        media_type=guess_image_content_type(data),
        headers={"Cache-Control": "private, max-age=604800, immutable"},
    )



@app.get("/api/media/{message_id}/encrypted-thumbnail", dependencies=[Depends(require_media_access)])
async def encrypted_thumbnail(
    message_id: int,
    account: str | None = Query(default=None, min_length=1, max_length=40),
    x_savedstream_device_key: str = Header(),
    device_cookie: str | None = Cookie(default=None, alias=DEVICE_COOKIE),
    cache: DiskCache = Depends(get_cache),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
    principal: AccessPrincipal = Depends(require_media_access),
) -> Response:
    device_key = await registered_device_public_key(x_savedstream_device_key, device_cookie, database)
    account = await authorized_account(account, principal, telegram)
    message, item = await telegram.get_media_message(account, message_id)
    if not item["has_thumbnail"]:
        raise MediaNotFound("The media has no thumbnail")
    cache_key = telegram.media_cache_key(message, item)
    data = await cache.get_thumbnail(cache_key, lambda: telegram.download_thumbnail(message))
    aad = f"thumbnail:{account}:{message_id}:{item['size']}".encode("utf-8")
    encrypted, crypto_headers = encrypt_for_device(data, device_key, aad)
    headers = {**crypto_headers, "X-SavedStream-Mime": guess_image_content_type(data), "Cache-Control": "private, no-store", "Content-Length": str(len(encrypted))}
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
    principal: AccessPrincipal = Depends(require_media_access),
) -> Response:
    device_key = await registered_device_public_key(x_savedstream_device_key, device_cookie, database)
    account = await authorized_account(account, principal, telegram)
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
    telegram: TeleBoxClient = Depends(get_telegram),
    principal: AccessPrincipal = Depends(require_media_access),
) -> StreamingResponse:
    account = await authorized_account(account, principal, telegram)
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

    async def body() -> AsyncIterator[bytes]:
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
            yield data
            position += len(data)

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
    cache: DiskCache = Depends(get_cache),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict:
    cache_limit = await database.get_cache_limit()
    cache_stats = await cache.stats()
    viewer_hash = await database.get_setting("viewer_key_hash", "")
    return {
        "cache_max_gb": round(cache_limit / (1024**3), 2),
        "cache_bytes": cache_stats["bytes"],
        "cache_files": cache_stats["files"],
        "access_restricted": await database.access_restricted(),
        "viewer_key_configured": bool(viewer_hash),
        "telegram": await telegram.status(),
        "accounts": (await telegram.accounts()).get("items", []),
        "helper_bot": await telegram.helper_bot_status(),
        "bindings": (await telegram.bindings()).get("items", []),
        "ingest_jobs": (await telegram.jobs()).get("items", []),
        "access_users": await database.list_media_users(),
    }


@app.put("/api/admin/helper-bot", dependencies=[Depends(require_admin)])
async def update_helper_bot(
    payload: HelperBotPayload,
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict:
    await telegram.set_helper_bot(payload.token.strip())
    return await telegram.helper_bot_status()


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
) -> dict:
    user = await database.set_media_user_status(telegram_user_id, payload.status)
    if not user:
        raise HTTPException(status_code=404, detail={"code": "ACCESS_USER_NOT_FOUND"})
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
    await telegram.get_media_message(account, message_id)
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

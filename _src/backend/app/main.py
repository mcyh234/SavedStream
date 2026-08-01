from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from .security import TokenSigner, constant_time_equal, hash_secret, verify_secret
from .telebox_client import (
    TELEGRAM_CHUNK_SIZE,
    MediaNotFound,
    TeleBoxClient,
    TelegramUnavailable,
    guess_image_content_type,
)


ADMIN_COOKIE = "savedstream_admin"
VIEWER_COOKIE = "savedstream_viewer"
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
    access_restricted: bool
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


async def require_viewer(
    database: Database = Depends(get_database),
    admin_cookie: str | None = Cookie(default=None, alias=ADMIN_COOKIE),
    viewer_cookie: str | None = Cookie(default=None, alias=VIEWER_COOKIE),
) -> None:
    if not await database.access_restricted():
        return
    if signer.verify(admin_cookie, "admin", "control"):
        return
    viewer_hash = await database.get_setting("viewer_key_hash", "")
    if viewer_hash and signer.verify(viewer_cookie, "viewer", viewer_hash):
        return
    raise HTTPException(status_code=401, detail={"code": "VIEWER_AUTH_REQUIRED"})
async def require_media_access(
    database: Database = Depends(get_database),
    admin_cookie: str | None = Cookie(default=None, alias=ADMIN_COOKIE),
    viewer_cookie: str | None = Cookie(default=None, alias=VIEWER_COOKIE),
) -> None:
    if signer.verify(admin_cookie, "admin", "control"):
        return
    viewer_hash = await database.get_setting("viewer_key_hash", "")
    if viewer_hash and signer.verify(viewer_cookie, "viewer", viewer_hash):
        return
    raise HTTPException(status_code=401, detail={"code": "MEDIA_AUTH_REQUIRED"})


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
    viewer_cookie: str | None = Cookie(default=None, alias=VIEWER_COOKIE),
) -> dict:
    tg_status = await telegram.status()
    restricted = await database.access_restricted()
    viewer_hash = await database.get_setting("viewer_key_hash", "")
    admin_authenticated = signer.verify(admin_cookie, "admin", "control")
    viewer_authenticated = (
        not restricted
        or admin_authenticated
        or bool(viewer_hash and signer.verify(viewer_cookie, "viewer", viewer_hash))
    )
    media_authenticated = admin_authenticated or bool(
        viewer_hash and signer.verify(viewer_cookie, "viewer", viewer_hash)
    )
    return {
        "configuration_ok": settings.configuration_ok,
        "telegram_authenticated": tg_status["authenticated"],
        "telegram_state": tg_status["state"],
        "telegram_error": tg_status["error"],
        "access_restricted": restricted,
        "viewer_authenticated": viewer_authenticated,
        "admin_authenticated": admin_authenticated,
        "media_authenticated": media_authenticated,
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
    payload: KeyPayload,
    response: Response,
    database: Database = Depends(get_database),
) -> dict[str, bool]:
    viewer_hash = await database.get_setting("viewer_key_hash", "")
    if not viewer_hash or not verify_secret(payload.key, viewer_hash):
        await asyncio.sleep(0.35)
        raise HTTPException(status_code=401, detail={"code": "INVALID_VIEWER_KEY"})
    token = signer.issue("viewer", viewer_hash, COOKIE_TTL)
    response.set_cookie(
        VIEWER_COOKIE,
        token,
        max_age=COOKIE_TTL,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return {"ok": True}


@app.post("/api/access/logout")
async def viewer_logout(response: Response) -> dict[str, bool]:
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
    account: str = Query(default=settings.telebox_default_account, min_length=1, max_length=40),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> dict:
    account = await telegram.resolve_account(account)
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
async def public_accounts(telegram: TeleBoxClient = Depends(get_telegram)) -> dict:
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
    account: str = Query(default=settings.telebox_default_account, min_length=1, max_length=40),
    size: int | None = Query(default=None, ge=1),
    cache: DiskCache = Depends(get_cache),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> Response:
    account = await telegram.resolve_account(account)
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
    account: str = Query(default=settings.telebox_default_account, min_length=1, max_length=40),
    x_savedstream_device_key: str = Header(),
    device_cookie: str | None = Cookie(default=None, alias=DEVICE_COOKIE),
    cache: DiskCache = Depends(get_cache),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> Response:
    device_key = await registered_device_public_key(x_savedstream_device_key, device_cookie, database)
    account = await telegram.resolve_account(account)
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
    account: str = Query(default=settings.telebox_default_account, min_length=1, max_length=40),
    x_savedstream_device_key: str = Header(),
    device_cookie: str | None = Cookie(default=None, alias=DEVICE_COOKIE),
    cache: DiskCache = Depends(get_cache),
    database: Database = Depends(get_database),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> Response:
    device_key = await registered_device_public_key(x_savedstream_device_key, device_cookie, database)
    account = await telegram.resolve_account(account)
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
    account: str = Query(default=settings.telebox_default_account, min_length=1, max_length=40),
    cache: DiskCache = Depends(get_cache),
    telegram: TeleBoxClient = Depends(get_telegram),
) -> StreamingResponse:
    account = await telegram.resolve_account(account)
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
    current_hash = await database.get_setting("viewer_key_hash", "")
    new_hash = current_hash
    if payload.clear_viewer_key:
        new_hash = ""
    if payload.viewer_key and payload.viewer_key.strip():
        if len(payload.viewer_key.strip()) < 8:
            raise HTTPException(status_code=422, detail="Viewer key must contain at least 8 characters")
        new_hash = hash_secret(payload.viewer_key.strip())
    if payload.access_restricted and not new_hash:
        raise HTTPException(status_code=422, detail="Set a viewer key before enabling access restriction")

    await database.update_access_settings(
        cache_max_bytes=int(payload.cache_max_gb * 1024**3),
        access_restricted=payload.access_restricted,
        viewer_key_hash=new_hash,
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
    account: str = Query(default=settings.telebox_default_account, min_length=1, max_length=40),
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

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError,
    SessionPasswordNeededError,
    UnauthorizedError,
)

from .config import Settings
from .media_metadata import infer_media_kind, normalize_media_mime_type, preferred_media_date


TELEGRAM_CHUNK_SIZE = 512 * 1024
MESSAGE_SCAN_LIMIT = 500
AUTHORIZATION_CHECK_INTERVAL = 10.0

AUTHORIZATION_ERRORS = (UnauthorizedError, AuthKeyDuplicatedError)


class TelegramUnavailable(RuntimeError):
    pass


class MediaNotFound(LookupError):
    pass


class TelegramService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: TelegramClient | None = None
        self.state = "configuration_required"
        self.last_error: str | None = None
        self._qr: Any | None = None
        self._qr_task: asyncio.Task[Any] | None = None
        self._client_lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock()
        self._authorization_lock = asyncio.Lock()
        self._last_authorization_check = 0.0

    async def initialize(self) -> None:
        if not self.settings.configuration_ok:
            self.state = "configuration_required"
            return
        await self._create_client()
        await self._refresh_authorization(force=True)

    async def close(self) -> None:
        await self._cancel_qr_waiter()
        if self.client and self.client.is_connected():
            await self.client.disconnect()

    async def status(self) -> dict[str, Any]:
        await self._refresh_authorization()
        expires_at = None
        if (
            self.state == "waiting_for_scan"
            and self._qr is not None
            and getattr(self._qr, "expires", None)
        ):
            expires_at = self._qr.expires.astimezone(timezone.utc).isoformat()
        return {
            "state": self.state,
            "authenticated": self.state == "authenticated",
            "expires_at": expires_at,
            "error": self.last_error,
        }

    async def start_qr_login(self) -> dict[str, Any]:
        if not self.settings.configuration_ok:
            raise TelegramUnavailable("Telegram API credentials are not configured")
        async with self._client_lock:
            await self._ensure_client()
            assert self.client is not None
            try:
                if await self.client.get_me() is not None:
                    self.state = "authenticated"
                    return await self.status()
            except AuthKeyDuplicatedError:
                await self._discard_invalid_client()
                await self._ensure_client()
                assert self.client is not None

            await self._cancel_qr_waiter()
            try:
                qr = await self.client.qr_login()
            except AuthKeyDuplicatedError:
                await self._discard_invalid_client()
                await self._ensure_client()
                assert self.client is not None
                qr = await self.client.qr_login()
            self._qr = qr
            self.state = "waiting_for_scan"
            self.last_error = None
            self._qr_task = asyncio.create_task(self._wait_for_qr(qr))
            return {
                "state": self.state,
                "authenticated": False,
                "url": qr.url,
                "expires_at": qr.expires.astimezone(timezone.utc).isoformat(),
                "error": None,
            }

    async def submit_password(self, password: str) -> dict[str, Any]:
        async with self._client_lock:
            if self.state != "password_required":
                raise TelegramUnavailable(
                    "A two-step verification password is not currently required"
                )
            await self._ensure_client()
            assert self.client is not None
            try:
                await self.client.sign_in(password=password)
            except Exception as exc:
                self.last_error = "The two-step verification password was rejected"
                raise TelegramUnavailable(self.last_error) from exc
            self.state = "authenticated"
            self.last_error = None
            self._last_authorization_check = asyncio.get_running_loop().time()
        return await self.status()

    async def logout(self, clear_session: bool = False) -> None:
        async with self._client_lock:
            await self._cancel_qr_waiter()
            if self.client:
                try:
                    if self.client.is_connected() and await self.client.is_user_authorized():
                        await self.client.log_out()
                    elif self.client.is_connected():
                        await self.client.disconnect()
                except Exception:
                    try:
                        await self.client.disconnect()
                    except Exception:
                        pass
            self.client = None
            if clear_session or self._session_files_exist():
                await asyncio.to_thread(self._delete_session_files)
            await self._create_client()
            self.state = "unauthenticated"
            self.last_error = None
            self._last_authorization_check = 0.0

    async def list_saved_media(
        self,
        *,
        limit: int,
        cursor: int | None,
        order: str,
        kind: str,
        query: str,
    ) -> tuple[list[dict[str, Any]], int | None, bool]:
        client = await self.authorized_client()
        reverse = order == "oldest"
        iterator_args: dict[str, Any] = {
            "limit": MESSAGE_SCAN_LIMIT + 1,
            "reverse": reverse,
        }
        if query:
            iterator_args["search"] = query
        if cursor:
            if reverse:
                iterator_args["min_id"] = cursor
            else:
                iterator_args["offset_id"] = cursor

        items: list[dict[str, Any]] = []
        last_scanned_id: int | None = None
        last_returned_id: int | None = None
        scanned = 0
        has_more = False
        try:
            async for message in client.iter_messages("me", **iterator_args):
                scanned += 1
                last_scanned_id = int(message.id)
                item = self._serialize_message(message)
                if item is not None and (kind == "all" or item["kind"] == kind):
                    if len(items) >= limit:
                        has_more = True
                        break
                    items.append(item)
                    last_returned_id = int(message.id)

                if scanned >= MESSAGE_SCAN_LIMIT:
                    has_more = True
                    break
        except AUTHORIZATION_ERRORS as exc:
            await self._authorization_lost(exc)
            raise TelegramUnavailable("Telegram login has expired; scan again") from exc

        if has_more:
            # When an extra matching item was consumed, resume after the last item
            # returned to the caller so that the extra item is not skipped.
            next_cursor = last_returned_id if len(items) >= limit else last_scanned_id
        else:
            next_cursor = None
        return items, next_cursor, has_more

    async def get_media_message(self, message_id: int) -> tuple[Any, dict[str, Any]]:
        client = await self.authorized_client()
        try:
            message = await client.get_messages("me", ids=message_id)
        except AUTHORIZATION_ERRORS as exc:
            await self._authorization_lost(exc)
            raise TelegramUnavailable("Telegram login has expired; scan again") from exc
        if not message:
            raise MediaNotFound("Saved Message does not exist")
        item = self._serialize_message(message)
        if item is None:
            raise MediaNotFound("The Saved Message has no downloadable media")
        return message, item

    async def download_thumbnail(self, message: Any) -> bytes:
        client = await self.authorized_client()
        media = message.media
        thumbs = []
        if getattr(media, "photo", None):
            thumbs = list(getattr(media.photo, "sizes", []) or [])
        elif getattr(media, "document", None):
            thumbs = list(getattr(media.document, "thumbs", []) or [])
        else:
            thumbs = list(getattr(media, "sizes", []) or getattr(media, "thumbs", []) or [])
        if not thumbs and not message.photo:
            raise MediaNotFound("The media has no thumbnail")
        thumb_index = -2 if len(thumbs) > 1 else -1
        try:
            data = await client.download_media(message, file=bytes, thumb=thumb_index)
        except AUTHORIZATION_ERRORS as exc:
            await self._authorization_lost(exc)
            raise TelegramUnavailable("Telegram login has expired; scan again") from exc
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise MediaNotFound("Telegram did not return a thumbnail")
        return bytes(data)

    async def download_chunk(self, message: Any, offset: int, file_size: int) -> bytes:
        if offset < 0 or offset >= file_size or offset % TELEGRAM_CHUNK_SIZE:
            raise ValueError("Telegram chunk offset must be aligned and inside the file")
        client = await self.authorized_client()
        buffer = bytearray()
        iterator = client.iter_download(
            message.media,
            offset=offset,
            limit=1,
            chunk_size=TELEGRAM_CHUNK_SIZE,
            request_size=TELEGRAM_CHUNK_SIZE,
            file_size=file_size,
        )
        try:
            async for data in iterator:
                buffer.extend(data)
        except AUTHORIZATION_ERRORS as exc:
            await self._authorization_lost(exc)
            raise TelegramUnavailable("Telegram login has expired; scan again") from exc
        finally:
            await iterator.close()

        expected_size = min(TELEGRAM_CHUNK_SIZE, file_size - offset)
        if len(buffer) != expected_size:
            raise TelegramUnavailable(
                f"Telegram returned an incomplete media chunk at offset {offset}"
            )
        return bytes(buffer)

    @staticmethod
    def media_cache_key(message: Any, item: dict[str, Any]) -> str:
        document = getattr(message, "document", None)
        photo = getattr(message, "photo", None)
        media_identity = getattr(document or photo or message.media, "id", None)
        edit_date = getattr(message, "edit_date", None)
        edit_stamp = int(edit_date.timestamp()) if edit_date else 0
        return f"{message.id}:{media_identity or 'media'}:{item['size']}:{edit_stamp}"

    async def authorized_client(self) -> TelegramClient:
        await self._refresh_authorization()
        if self.state != "authenticated" or self.client is None:
            raise TelegramUnavailable("Telegram login is required")
        return self.client

    async def _create_client(self) -> None:
        if not self.settings.configuration_ok:
            return
        async with self._connection_lock:
            if self.client is not None:
                if not self.client.is_connected():
                    await self.client.connect()
                return
            session_path = self.settings.telegram_session_path
            session_path.parent.mkdir(parents=True, exist_ok=True)
            client = TelegramClient(
                str(session_path),
                self.settings.api_id,
                self.settings.api_hash,
                device_model="SavedStream Web",
                system_version="Linux",
                app_version="0.1.0",
                lang_code="zh-hans",
            )
            await client.connect()
            self.client = client

    async def _ensure_client(self) -> None:
        await self._create_client()
        assert self.client is not None

    async def _refresh_authorization(self, force: bool = False) -> None:
        loop_time = asyncio.get_running_loop().time()
        if (
            not force
            and loop_time - self._last_authorization_check
            < AUTHORIZATION_CHECK_INTERVAL
        ):
            return
        if not self.settings.configuration_ok:
            self.state = "configuration_required"
            return
        async with self._authorization_lock:
            loop_time = asyncio.get_running_loop().time()
            if (
                not force
                and loop_time - self._last_authorization_check
                < AUTHORIZATION_CHECK_INTERVAL
            ):
                return
            try:
                await self._ensure_client()
                assert self.client is not None
                if await self.client.get_me() is not None:
                    self.state = "authenticated"
                    self.last_error = None
                elif self.state not in {
                    "waiting_for_scan",
                    "password_required",
                    "qr_expired",
                }:
                    self.state = "unauthenticated"
                    self.last_error = None
            except AUTHORIZATION_ERRORS as exc:
                await self._authorization_lost(exc)
            except Exception as exc:
                self.state = "error"
                self.last_error = f"Telegram connection failed: {type(exc).__name__}"
            finally:
                self._last_authorization_check = asyncio.get_running_loop().time()

    async def _wait_for_qr(self, qr: Any) -> None:
        try:
            await qr.wait()
            if self._qr is qr:
                self.state = "authenticated"
                self.last_error = None
                self._last_authorization_check = asyncio.get_running_loop().time()
        except SessionPasswordNeededError:
            if self._qr is qr:
                self.state = "password_required"
                self.last_error = None
        except (asyncio.TimeoutError, TimeoutError):
            if self._qr is qr:
                self.state = "qr_expired"
                self.last_error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._qr is qr:
                self.state = "error"
                self.last_error = f"QR login failed: {type(exc).__name__}"
        finally:
            if self._qr is qr and self.state != "waiting_for_scan":
                self._qr = None

    async def _cancel_qr_waiter(self) -> None:
        task = self._qr_task
        self._qr_task = None
        self._qr = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _authorization_lost(self, exc: BaseException) -> None:
        self.state = "unauthenticated"
        self.last_error = "The Telegram session is no longer valid. Scan again."
        if isinstance(exc, AuthKeyDuplicatedError):
            await self._discard_invalid_client()

    async def _discard_invalid_client(self) -> None:
        async with self._connection_lock:
            client = self.client
            self.client = None
            if client and client.is_connected():
                try:
                    await client.disconnect()
                except Exception:
                    pass
            await asyncio.to_thread(self._delete_session_files)

    def _session_files_exist(self) -> bool:
        base = Path(f"{self.settings.telegram_session_path}.session")
        return any(
            Path(f"{base}{suffix}").exists()
            for suffix in ("", "-journal", "-shm", "-wal")
        )

    def _serialize_message(self, message: Any) -> dict[str, Any] | None:
        if not getattr(message, "media", None) or not getattr(message, "file", None):
            return None
        file_info = message.file
        size = int(getattr(file_info, "size", 0) or 0)
        if size <= 0:
            return None
        filename = str(getattr(file_info, "name", "") or "").strip()
        if not filename:
            extension = str(getattr(file_info, "ext", "") or "")
            filename = f"saved-{message.id}{extension}"
        mime_type = normalize_media_mime_type(
            str(getattr(file_info, "mime_type", "") or "application/octet-stream"),
            filename,
        )
        kind = self._media_kind(message, mime_type, filename)
        caption = (getattr(message, "raw_text", "") or "").strip()
        first_line = caption.splitlines()[0].strip() if caption else ""
        original_title = first_line or filename
        duration = getattr(file_info, "duration", None)
        width = getattr(file_info, "width", None)
        height = getattr(file_info, "height", None)
        date = getattr(message, "date", None) or datetime.now(timezone.utc)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        resolved_date = preferred_media_date(filename, date, kind)
        if isinstance(resolved_date, datetime):
            date_value = resolved_date.astimezone(timezone.utc).isoformat()
        else:
            date_value = str(resolved_date or date.astimezone(timezone.utc).isoformat())
        media = message.media
        has_thumbnail = bool(
            message.photo
            or getattr(media, "sizes", None)
            or (getattr(media, "document", None) and getattr(media.document, "thumbs", None))
        )
        return {
            "id": int(message.id),
            "kind": kind,
            "mime_type": mime_type,
            "size": size,
            "filename": filename,
            "original_title": original_title[:300],
            "caption": caption[:2000],
            "date": date_value,
            "duration": int(duration) if duration is not None else None,
            "width": int(width) if width is not None else None,
            "height": int(height) if height is not None else None,
            "has_thumbnail": has_thumbnail,
        }

    @staticmethod
    def _media_kind(message: Any, mime_type: str, filename: str = "") -> str:
        transport_kind = (
            "video"
            if getattr(message, "video", None)
            else "image"
            if getattr(message, "photo", None)
            else "audio"
            if getattr(message, "audio", None)
            else "file"
        )
        return infer_media_kind(transport_kind, mime_type, filename)

    def _delete_session_files(self) -> None:
        base = Path(f"{self.settings.telegram_session_path}.session")
        for suffix in ("", "-journal", "-shm", "-wal"):
            path = Path(f"{base}{suffix}")
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def guess_image_content_type(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable, AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings


TELEGRAM_CHUNK_SIZE = 512 * 1024


class TelegramUnavailable(RuntimeError):
    pass


class MediaNotFound(LookupError):
    pass


class InvalidWebLoginCode(ValueError):
    pass


class UploadQuotaExceeded(RuntimeError):
    def __init__(self, detail: dict[str, Any] | str) -> None:
        self.detail = detail
        super().__init__(str(detail))


class TeleBoxClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: httpx.AsyncClient | None = None

    async def initialize(self) -> None:
        self.client = httpx.AsyncClient(
            base_url=self.settings.telebox_url,
            headers={"Authorization": f"Bearer {self.settings.telebox_api_token}"},
            timeout=httpx.Timeout(30.0, read=120.0),
        )

    async def close(self) -> None:
        if self.client:
            await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self.client:
            raise TelegramUnavailable("TeleBox client is not initialized")
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise TelegramUnavailable(f"TeleBox unavailable: {exc}") from exc
        if response.status_code == 404:
            raise MediaNotFound("Media was not found")
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise TelegramUnavailable(f"TeleBox request failed: {detail}")
        return response

    async def accounts(self) -> dict[str, Any]:
        return (await self._request("GET", "/v1/accounts")).json()

    async def resolve_account(self, account_id: str) -> str:
        items = (await self.accounts()).get("items", [])
        if any(item.get("id") == account_id for item in items):
            return account_id
        fallback = next((item for item in items if item.get("state") == "authenticated"), items[0] if items else None)
        if not fallback:
            raise TelegramUnavailable("No TeleBox account is configured")
        return str(fallback["id"])

    async def create_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        return (await self._request("POST", "/v1/accounts", json=payload)).json()

    async def start_account_qr_login(self, account_id: str) -> dict[str, Any]:
        return (await self._request("POST", f"/v1/accounts/{quote(account_id)}/login/qr")).json()

    async def account_login_status(self, account_id: str) -> dict[str, Any]:
        return (await self._request("GET", f"/v1/accounts/{quote(account_id)}/login")).json()

    async def account_health(self, account_id: str) -> dict[str, Any]:
        return (await self._request("GET", f"/v1/accounts/{quote(account_id)}/health")).json()

    async def replication_copy(
        self,
        *,
        source_account_id: str,
        target_account_id: str,
        source_message_id: int,
        logical_media_id: str,
        fingerprint: str,
        filename: str,
        mime_type: str,
        caption: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return (
            await self._request(
                "POST",
                "/v1/replication/copy",
                json={
                    "source_account_id": source_account_id,
                    "target_account_id": target_account_id,
                    "source_message_id": int(source_message_id),
                    "logical_media_id": logical_media_id,
                    "fingerprint": fingerprint,
                    "filename": filename,
                    "mime_type": mime_type,
                    "caption": caption,
                    "idempotency_key": idempotency_key,
                },
            )
        ).json()

    async def replication_mutation(self, payload: dict[str, Any]) -> dict[str, Any]:
        return (await self._request("POST", "/v1/replication/mutation", json=payload)).json()

    async def find_replication(self, account_id: str, *, marker: str | None = None, fingerprint: str | None = None) -> dict[str, Any]:
        params: dict[str, str] = {}
        if marker:
            params["marker"] = marker
        if fingerprint:
            params["fingerprint"] = fingerprint
        return (await self._request("GET", f"/v1/accounts/{quote(account_id)}/replication/find", params=params)).json()

    async def cancel_account_login(self, account_id: str) -> dict[str, Any]:
        return (await self._request("DELETE", f"/v1/accounts/{quote(account_id)}/login")).json()

    async def status(self, account_id: str | None = None) -> dict[str, Any]:
        account = account_id or self.settings.telebox_default_account
        try:
            payload = await self.accounts()
            item = next((row for row in payload.get("items", []) if row.get("id") == account), None)
            if item is None:
                item = next((row for row in payload.get("items", []) if row.get("state") == "authenticated"), None)
            state = item.get("state", "unauthenticated") if item else "configuration_required"
            return {"state": state, "authenticated": state == "authenticated", "expires_at": None, "error": item.get("error") if item else None}
        except TelegramUnavailable as exc:
            return {"state": "error", "authenticated": False, "expires_at": None, "error": str(exc)}

    async def start_qr_login(self) -> dict[str, Any]:
        raise TelegramUnavailable("Add a TeleBox account with a StringSession before logging in")

    async def submit_password(self, password: str) -> dict[str, Any]:
        raise TelegramUnavailable("Two-step login is managed by TeleBox")

    async def logout(self, clear_session: bool = False) -> None:
        raise TelegramUnavailable("Account lifecycle is managed by TeleBox")

    async def list_saved_media(self, *, account_id: str, limit: int, cursor: str | int | None, order: str, kind: str, query: str) -> tuple[list[dict[str, Any]], str | int | None, bool]:
        params = {"limit": limit, "order": order, "kind": kind, "q": query}
        if cursor:
            params["cursor"] = cursor
        payload = (await self._request("GET", f"/v1/accounts/{quote(account_id)}/media", params=params)).json()
        return payload["items"], payload.get("next_cursor"), bool(payload.get("has_more"))

    async def sync_saved_media(
        self,
        *,
        account_id: str,
        mode: str,
        cursor: int | None = None,
        after_id: int | None = None,
        limit: int = 200,
        order: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"mode": mode, "limit": max(1, min(500, int(limit)))}
        if cursor:
            params["cursor"] = cursor
        if after_id:
            params["after_id"] = after_id
        if order in {"oldest", "newest"}:
            params["order"] = order
        return (await self._request("GET", f"/v1/accounts/{quote(account_id)}/media/sync", params=params)).json()

    async def get_media_message(self, account_id: str, message_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        item = (await self._request("GET", f"/v1/accounts/{quote(account_id)}/media/{message_id}")).json()
        return {"account_id": account_id, "id": message_id}, item

    async def download_thumbnail(self, message: dict[str, Any]) -> bytes:
        response = await self._request("GET", f"/v1/accounts/{quote(message['account_id'])}/media/{message['id']}/thumbnail")
        return response.content

    async def download_chunk(self, message: dict[str, Any], offset: int, file_size: int) -> bytes:
        end = min(file_size - 1, offset + TELEGRAM_CHUNK_SIZE - 1)
        response = await self._request("GET", f"/v1/accounts/{quote(message['account_id'])}/media/{message['id']}/stream", headers={"Range": f"bytes={offset}-{end}"})
        return response.content

    @staticmethod
    def media_cache_key(message: dict[str, Any], item: dict[str, Any]) -> str:
        return f"{message['account_id']}:{message['id']}:{item['size']}"

    async def helper_bot_status(self) -> dict[str, Any]:
        return (await self._request("GET", "/v1/helper-bot/status")).json()

    async def set_helper_bot(self, token: str) -> dict[str, Any]:
        return (await self._request("PUT", "/v1/helper-bot", json={"token": token})).json()

    async def consume_web_login_code(self, code: str) -> dict[str, Any]:
        if not self.client:
            raise TelegramUnavailable("TeleBox client is not initialized")
        try:
            response = await self.client.post("/v1/web-login/consume", json={"code": code})
        except httpx.HTTPError as exc:
            raise TelegramUnavailable(f"TeleBox unavailable: {exc}") from exc
        if response.status_code == 401:
            raise InvalidWebLoginCode("The Telegram login code is invalid, expired, or already used")
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise TelegramUnavailable(f"TeleBox request failed: {detail}")
        return response.json()

    async def create_invite(self, account_id: str) -> dict[str, Any]:
        return (await self._request("POST", f"/v1/accounts/{quote(account_id)}/invites")).json()

    async def bindings(self) -> dict[str, Any]:
        return (await self._request("GET", "/v1/bindings")).json()

    async def delete_binding(self, telegram_user_id: str) -> dict[str, Any]:
        return (await self._request("DELETE", "/v1/bindings", json={"telegram_user_id": telegram_user_id})).json()

    async def set_binding_status(
        self,
        telegram_user_id: str,
        *,
        enabled: bool,
        banned: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return (
            await self._request(
                "PUT",
                f"/v1/bindings/{quote(str(telegram_user_id))}",
                json={"enabled": bool(enabled), "banned": bool(banned), "reason": reason},
            )
        ).json()

    async def jobs(
        self,
        *,
        status: str | None = None,
        updated_after: int | None = None,
        after_job_id: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if updated_after is not None:
            params["updated_after"] = max(0, int(updated_after))
        if after_job_id is not None:
            params["after_job_id"] = max(0, int(after_job_id))
        if limit is not None:
            params["limit"] = max(1, min(500, int(limit)))
        return (await self._request("GET", "/v1/ingest/jobs", params=params or None)).json()

    async def retry_job(self, job_id: int) -> dict[str, Any]:
        return (await self._request("POST", f"/v1/ingest/jobs/{job_id}/retry")).json()

    async def cancel_user_ingest_jobs(self, telegram_user_id: str, *, reason: str | None = None) -> dict[str, Any]:
        return (
            await self._request(
                "POST",
                f"/v1/ingest/users/{quote(str(telegram_user_id))}/cancel",
                json={"reason": reason},
            )
        ).json()

    async def update_ingest_job_review(
        self,
        job_id: int,
        *,
        decision: str,
        reason: str | None = None,
        reviewed_by: str = "admin",
    ) -> dict[str, Any]:
        return (
            await self._request(
                "PATCH",
                f"/v1/ingest/jobs/{int(job_id)}/review",
                json={
                    "decision": decision,
                    "reason": reason,
                    "reviewed_by": reviewed_by,
                },
            )
        ).json()

    async def delete_ingest_job(
        self,
        job_id: int,
        *,
        reason: str | None = None,
        deleted_by: str = "admin",
    ) -> dict[str, Any]:
        return (
            await self._request(
                "DELETE",
                f"/v1/ingest/jobs/{int(job_id)}",
                json={"reason": reason, "deleted_by": deleted_by},
            )
        ).json()

    async def delete_media(
        self,
        account_id: str,
        message_id: int,
        *,
        reason: str | None = None,
        deleted_by: str = "admin",
    ) -> dict[str, Any]:
        return (
            await self._request(
                "DELETE",
                f"/v1/accounts/{quote(account_id)}/media/{int(message_id)}",
                json={"reason": reason, "deleted_by": deleted_by},
            )
        ).json()

    async def helper_bot_rate_limit(self) -> dict[str, Any]:
        return (await self._request("GET", "/v1/helper-bot/rate-limit")).json()

    async def set_helper_bot_rate_limit(self, payload: dict[str, Any]) -> dict[str, Any]:
        return (await self._request("PUT", "/v1/helper-bot/rate-limit", json=payload)).json()

    async def reserve_upload_quota(
        self,
        *,
        telegram_user_id: str,
        batch_id: str,
        file_count: int,
        total_bytes: int,
    ) -> dict[str, Any]:
        if not self.client:
            raise TelegramUnavailable("TeleBox client is not initialized")
        try:
            response = await self.client.post(
                "/v1/upload-quota/reservations",
                json={
                    "telegram_user_id": str(telegram_user_id),
                    "batch_id": str(batch_id),
                    "file_count": int(file_count),
                    "total_bytes": int(total_bytes),
                },
            )
        except httpx.HTTPError as exc:
            raise TelegramUnavailable(f"TeleBox unavailable: {exc}") from exc
        if response.status_code == 429:
            try:
                detail = response.json().get("detail", response.json())
            except ValueError:
                detail = response.text
            raise UploadQuotaExceeded(detail)
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise TelegramUnavailable(f"TeleBox request failed: {detail}")
        return response.json()

    async def complete_upload_quota(self, reservation_key: str) -> dict[str, Any]:
        return (
            await self._request("POST", f"/v1/upload-quota/reservations/{quote(str(reservation_key))}/complete")
        ).json()

    async def release_upload_quota(self, reservation_key: str) -> dict[str, Any]:
        return (
            await self._request("DELETE", f"/v1/upload-quota/reservations/{quote(str(reservation_key))}")
        ).json()

    async def upload_file(
        self,
        *,
        account_id: str,
        file_path: Path,
        filename: str,
        mime_type: str,
        caption: str = "",
        progress_callback: Callable[[int, int], Awaitable[None] | None] | None = None,
    ) -> dict[str, Any]:
        if not self.client:
            raise TelegramUnavailable("TeleBox client is not initialized")
        total = file_path.stat().st_size
        encoded_name = base64.urlsafe_b64encode(filename.encode("utf-8")).decode("ascii").rstrip("=")
        encoded_caption = base64.urlsafe_b64encode(caption.encode("utf-8")).decode("ascii").rstrip("=")

        async def body() -> AsyncIterator[bytes]:
            sent = 0
            with file_path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    sent += len(chunk)
                    if progress_callback:
                        result = progress_callback(sent, total)
                        if result is not None:
                            await result
                    yield chunk

        try:
            response = await self.client.post(
                f"/v1/accounts/{quote(account_id)}/upload",
                content=body(),
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(total),
                    "X-Upload-Filename": encoded_name,
                    "X-Upload-Mime": mime_type[:200],
                    "X-Upload-Caption": encoded_caption,
                },
                timeout=None,
            )
        except httpx.HTTPError as exc:
            raise TelegramUnavailable(f"TeleBox unavailable: {exc}") from exc
        if response.status_code == 404:
            raise MediaNotFound("Uploaded media was not found")
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise TelegramUnavailable(f"TeleBox request failed: {detail}")
        return response.json()

    async def export_system_backup(self) -> dict[str, Any]:
        """Export TeleBox-owned state through its internal bridge API."""
        return (await self._request("GET", "/v1/system-backups/export")).json()

    async def import_system_backup(self, payload: dict[str, Any]) -> dict[str, Any]:
        return (await self._request("POST", "/v1/system-backups/import", json=payload)).json()

    async def list_system_backups(self, *, account_id: str, limit: int = 200) -> list[dict[str, Any]]:
        payload = (await self._request("GET", f"/v1/accounts/{quote(account_id)}/system-backups", params={"limit": max(1, min(500, limit))})).json()
        return list(payload.get("items", []))


def guess_image_content_type(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"

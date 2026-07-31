from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings


TELEGRAM_CHUNK_SIZE = 512 * 1024


class TelegramUnavailable(RuntimeError):
    pass


class MediaNotFound(LookupError):
    pass


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

    async def list_saved_media(self, *, account_id: str, limit: int, cursor: int | None, order: str, kind: str, query: str) -> tuple[list[dict[str, Any]], int | None, bool]:
        params = {"limit": limit, "order": order, "kind": kind, "q": query}
        if cursor:
            params["cursor"] = cursor
        payload = (await self._request("GET", f"/v1/accounts/{quote(account_id)}/media", params=params)).json()
        return payload["items"], payload.get("next_cursor"), bool(payload.get("has_more"))

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

    async def create_invite(self, account_id: str) -> dict[str, Any]:
        return (await self._request("POST", f"/v1/accounts/{quote(account_id)}/invites")).json()

    async def bindings(self) -> dict[str, Any]:
        return (await self._request("GET", "/v1/bindings")).json()

    async def delete_binding(self, telegram_user_id: str) -> dict[str, Any]:
        return (await self._request("DELETE", "/v1/bindings", json={"telegram_user_id": telegram_user_id})).json()

    async def jobs(self) -> dict[str, Any]:
        return (await self._request("GET", "/v1/ingest/jobs")).json()

    async def retry_job(self, job_id: int) -> dict[str, Any]:
        return (await self._request("POST", f"/v1/ingest/jobs/{job_id}/retry")).json()


def guess_image_content_type(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"

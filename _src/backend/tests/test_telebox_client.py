from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from app.config import Settings
from app.telebox_client import TELEGRAM_CHUNK_SIZE, TeleBoxClient


def make_settings() -> Settings:
    return Settings(api_id=0, api_hash="", admin_key="admin", data_dir=Path("."), cookie_secure=False, session_cookie_days=30, telebox_url="http://telebox.test", telebox_api_token="internal-token")


def test_resolve_account_falls_back_to_authenticated_profile() -> None:
    async def verify() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": [{"id": "alpha", "state": "authenticated"}]})

        service = TeleBoxClient(make_settings())
        service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://telebox.test")
        try:
            assert await service.resolve_account("missing") == "alpha"
        finally:
            await service.close()

    asyncio.run(verify())


def test_download_chunk_uses_aligned_range() -> None:
    async def verify() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Range"] == f"bytes={TELEGRAM_CHUNK_SIZE}-{2 * TELEGRAM_CHUNK_SIZE - 1}"
            return httpx.Response(206, content=b"chunk")

        service = TeleBoxClient(make_settings())
        service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://telebox.test")
        try:
            data = await service.download_chunk({"account_id": "alpha", "id": 7}, TELEGRAM_CHUNK_SIZE, 4 * TELEGRAM_CHUNK_SIZE)
            assert data == b"chunk"
        finally:
            await service.close()

    asyncio.run(verify())

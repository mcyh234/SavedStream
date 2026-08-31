from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.auth import AuthStore
from app.database import Database
from app.main import app, get_auth, get_database, get_telegram


class _AnonymousTelegramProbe:
    async def status(self):
        raise AssertionError("anonymous /api/status must not query Telegram")

    async def helper_bot_status(self):
        raise AssertionError("anonymous /api/status must not query Helper Bot")


@pytest.mark.asyncio
async def test_anonymous_status_omits_operational_telegram_details(tmp_path: Path) -> None:
    database = Database(tmp_path / "security.db")
    await database.initialize()
    auth = AuthStore(database.path)
    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_auth] = lambda: auth
    app.dependency_overrides[get_telegram] = lambda: _AnonymousTelegramProbe()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            response = await client.get("/api/status")
            assert response.status_code == 200
            body = response.json()
            assert body["access_status"] == "unauthenticated"
            assert "telegram_authenticated" not in body
            assert "telegram_state" not in body
            assert "telegram_error" not in body
            assert "helper_bot_username" not in body
            assert "public_key_configured" not in body
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_security_headers_and_metadata_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURITY_CONTACT", "mailto:security@example.invalid")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.headers["strict-transport-security"].startswith("max-age=")
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert "camera=()" in response.headers["permissions-policy"]

        sitemap = await client.get("/sitemap.xml")
        assert sitemap.status_code == 200
        assert sitemap.headers["content-type"].startswith("application/xml")
        assert "<urlset" in sitemap.text

        security = await client.get("/.well-known/security.txt")
        assert security.status_code == 200
        assert security.headers["content-type"].startswith("text/plain")
        assert "Contact:" in security.text
        assert "Expires:" in security.text

        robots = await client.get("/robots.txt")
        assert robots.status_code == 200
        assert robots.headers["content-type"].startswith("text/plain")
        assert "Disallow: /api/" in robots.text


@pytest.mark.asyncio
async def test_unknown_api_path_is_structured_json_404() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        response = await client.get("/api/does-not-exist")
        assert response.status_code == 404
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "detail": {"code": "API_NOT_FOUND", "path": "/api/does-not-exist"},
            "code": "API_NOT_FOUND",
        }

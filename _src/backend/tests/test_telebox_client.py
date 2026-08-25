from __future__ import annotations

import asyncio
import base64
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


def test_sync_saved_media_preserves_mode_and_pagination_parameters() -> None:
    async def verify() -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"items": [], "next_cursor": 17, "has_more": True})

        service = TeleBoxClient(make_settings())
        service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://telebox.test")
        try:
            full = await service.sync_saved_media(account_id="alpha", mode="full", cursor=42, limit=200)
            incremental = await service.sync_saved_media(account_id="alpha", mode="incremental", after_id=99, limit=20)
            assert full["next_cursor"] == 17
            assert requests[0].url.path == "/v1/accounts/alpha/media/sync"
            assert requests[0].url.params["mode"] == "full"
            assert requests[0].url.params["cursor"] == "42"
            assert "after_id" not in requests[0].url.params
            assert requests[1].url.params["mode"] == "incremental"
            assert requests[1].url.params["after_id"] == "99"
            assert requests[1].url.params["limit"] == "20"
            assert incremental["has_more"] is True
        finally:
            await service.close()

    asyncio.run(verify())


def test_completed_jobs_request_sends_incremental_cursor() -> None:
    async def verify() -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"items": [], "has_more": False})

        service = TeleBoxClient(make_settings())
        service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://telebox.test")
        try:
            await service.jobs(status="completed", updated_after=1234, after_job_id=9, limit=50)
        finally:
            await service.close()

        request = captured[0]
        assert request.url.path == "/v1/ingest/jobs"
        assert request.url.params["status"] == "completed"
        assert request.url.params["updated_after"] == "1234"
        assert request.url.params["after_job_id"] == "9"
        assert request.url.params["limit"] == "50"

    asyncio.run(verify())


def test_upload_file_streams_exact_body_and_reports_progress(tmp_path: Path) -> None:
    async def verify() -> None:
        payload = b"telegram-upload-payload" * 10
        source = tmp_path / "movie name.mp4"
        source.write_bytes(payload)
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = request.content
            captured["length"] = request.headers["content-length"]
            captured["filename"] = base64.urlsafe_b64decode(
                request.headers["x-upload-filename"] + "=="
            ).decode("utf-8")
            captured["mime"] = request.headers["x-upload-mime"]
            return httpx.Response(201, json={"id": 123, "size": len(payload)})

        progress: list[tuple[int, int]] = []

        async def on_progress(sent: int, total: int) -> None:
            progress.append((sent, total))

        service = TeleBoxClient(make_settings())
        service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://telebox.test")
        try:
            result = await service.upload_file(
                account_id="alpha",
                file_path=source,
                filename=source.name,
                mime_type="video/mp4",
                progress_callback=on_progress,
            )
        finally:
            await service.close()

        assert result["id"] == 123
        assert captured == {
            "path": "/v1/accounts/alpha/upload",
            "body": payload,
            "length": str(len(payload)),
            "filename": "movie name.mp4",
            "mime": "video/mp4",
        }
        assert progress == [(len(payload), len(payload))]

    asyncio.run(verify())


def test_review_and_helper_rate_limit_requests_are_proxied() -> None:
    async def verify() -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            if request.url.path.endswith("/rate-limit"):
                return httpx.Response(200, json={"max_file_bytes": 2000})
            return httpx.Response(200, json={"id": 7, "review_status": "approved"})

        service = TeleBoxClient(make_settings())
        service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://telebox.test")
        try:
            review = await service.update_ingest_job_review(7, decision="approved", reason="ok")
            limits = await service.helper_bot_rate_limit()
            saved = await service.set_helper_bot_rate_limit({"max_file_bytes": 2000})
        finally:
            await service.close()

        assert review["review_status"] == "approved"
        assert limits["max_file_bytes"] == 2000
        assert saved["max_file_bytes"] == 2000
        assert captured[0].method == "PATCH"
        assert captured[0].url.path == "/v1/ingest/jobs/7/review"
        assert captured[1].method == "GET"
        assert captured[2].method == "PUT"

    asyncio.run(verify())


def test_delete_and_binding_ban_requests_are_proxied() -> None:
    async def verify() -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        service = TeleBoxClient(make_settings())
        service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://telebox.test")
        try:
            await service.set_binding_status("100", enabled=False, banned=True, reason="违规")
            await service.delete_ingest_job(7, reason="违规", deleted_by="admin")
            await service.delete_media("alpha", 42, reason="违规", deleted_by="admin")
        finally:
            await service.close()

        assert [request.method for request in captured] == ["PUT", "DELETE", "DELETE"]
        assert captured[0].url.path == "/v1/bindings/100"
        assert captured[1].url.path == "/v1/ingest/jobs/7"
        assert captured[2].url.path == "/v1/accounts/alpha/media/42"

    asyncio.run(verify())


def test_system_backup_bridge_methods_are_proxied() -> None:
    async def verify() -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            if request.url.path.endswith("/export"):
                return httpx.Response(200, json={"files": []})
            if request.url.path.endswith("/system-backups"):
                return httpx.Response(200, json={"items": [{"id": 3}]})
            return httpx.Response(200, json={"ok": True})

        service = TeleBoxClient(make_settings())
        service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://telebox.test")
        try:
            assert await service.export_system_backup() == {"files": []}
            assert await service.import_system_backup({"files": []}) == {"ok": True}
            assert await service.list_system_backups(account_id="alpha") == [{"id": 3}]
        finally:
            await service.close()
        assert [request.method for request in captured] == ["GET", "POST", "GET"]
        assert captured[0].url.path == "/v1/system-backups/export"
        assert captured[1].url.path == "/v1/system-backups/import"
        assert captured[2].url.path == "/v1/accounts/alpha/system-backups"

    asyncio.run(verify())

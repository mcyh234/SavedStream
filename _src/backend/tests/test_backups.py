from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app import main as main_module
from app.backups import cleanup_backups, delete_backup, list_backups
from app.database import Database
from app.main import ADMIN_COOKIE, app, get_database, signer


def make_backup(root: Path, stamp: str, *, code_bytes: int = 0, volume_bytes: int = 0) -> None:
    code = root / f"code-{stamp}"
    volumes = root / f"volumes-{stamp}"
    code.mkdir(parents=True)
    volumes.mkdir(parents=True)
    (code / "_src").mkdir()
    (code / "TeleBox").mkdir()
    (code / "_src" / "main.py").write_bytes(b"x" * code_bytes)
    (volumes / "savedstream-data.tgz").write_bytes(b"y" * volume_bytes)


def test_list_backups_aggregates_pairs_and_sizes(tmp_path: Path) -> None:
    make_backup(tmp_path, "20260801-120000", code_bytes=100, volume_bytes=200)
    make_backup(tmp_path, "20260802-120000", code_bytes=50, volume_bytes=0)
    # Unrelated entries are ignored.
    (tmp_path / "failed-123").mkdir()
    (tmp_path / "notes.txt").write_text("hello")

    result = list_backups(tmp_path)
    assert result["configured"] is True
    stamps = [item["stamp"] for item in result["items"]]
    assert stamps == ["20260802-120000", "20260801-120000"]
    newest = result["items"][0]
    assert newest["size_bytes"] == 50
    assert newest["has_code"] is True and newest["has_volumes"] is True
    assert newest["code_files"] == ["main.py"]
    assert newest["volume_files"] == ["savedstream-data.tgz"]
    oldest = result["items"][1]
    assert oldest["size_bytes"] == 300


def test_list_backups_handles_missing_directory(tmp_path: Path) -> None:
    result = list_backups(tmp_path / "does-not-exist")
    assert result["configured"] is False
    assert result["items"] == []


def test_delete_backup_removes_pair_and_rejects_bad_stamps(tmp_path: Path) -> None:
    make_backup(tmp_path, "good-stamp_1", code_bytes=10, volume_bytes=20)
    deleted = delete_backup(tmp_path, "good-stamp_1")
    assert deleted["removed"] == ["code-good-stamp_1", "volumes-good-stamp_1"]
    assert deleted["freed_bytes"] == 30
    assert not (tmp_path / "code-good-stamp_1").exists()
    assert not (tmp_path / "volumes-good-stamp_1").exists()

    with pytest.raises(FileNotFoundError):
        delete_backup(tmp_path, "good-stamp_1")
    with pytest.raises(ValueError):
        delete_backup(tmp_path, "../escape")
    with pytest.raises(ValueError):
        delete_backup(tmp_path, "bad/stamp")


def test_cleanup_backups_keeps_newest_and_supports_dry_run(tmp_path: Path) -> None:
    for index, stamp in enumerate(["20260801-000000", "20260802-000000", "20260803-000000"], start=1):
        make_backup(tmp_path, stamp, code_bytes=index * 100, volume_bytes=index * 100)
        # Prefer a stable, ascending mtime order where the platform supports
        # setting directory timestamps; the stamp tie-breaker keeps the
        # assertion deterministic either way.
        future = time.time() + index * 60
        try:
            os.utime(tmp_path / f"code-{stamp}", (future, future))
            os.utime(tmp_path / f"volumes-{stamp}", (future, future))
        except OSError:
            pass

    preview = cleanup_backups(tmp_path, keep=1, dry_run=True)
    assert preview["dry_run"] is True
    assert [item["stamp"] for item in preview["removed"]] == ["20260802-000000", "20260801-000000"]
    assert preview["freed_bytes"] == 400 + 200
    assert (tmp_path / "code-20260802-000000").exists()

    result = cleanup_backups(tmp_path, keep=1)
    assert [item["stamp"] for item in result["removed"]] == ["20260802-000000", "20260801-000000"]
    assert not (tmp_path / "code-20260802-000000").exists()
    assert not (tmp_path / "volumes-20260801-000000").exists()
    assert (tmp_path / "code-20260803-000000").exists()


@pytest.mark.asyncio
async def test_backup_admin_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_backup(tmp_path, "20260801-000000", code_bytes=100, volume_bytes=100)
    make_backup(tmp_path, "20260802-000000", code_bytes=200, volume_bytes=200)
    monkeypatch.setattr(main_module, "settings", replace(main_module.settings, backups_dir=tmp_path))

    database = Database(tmp_path / "api.db")
    await database.initialize()
    app.dependency_overrides[get_database] = lambda: database
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            unauth = await client.get("/api/admin/backups")
            assert unauth.status_code == 401

            client.cookies.set(ADMIN_COOKIE, signer.issue("admin", "control", 60))
            listing = await client.get("/api/admin/backups")
            assert listing.status_code == 200
            body = listing.json()
            assert body["configured"] is True
            assert [item["stamp"] for item in body["items"]] == ["20260802-000000", "20260801-000000"]

            preview = await client.post(
                "/api/admin/backups/cleanup", json={"keep": 1, "dry_run": True}
            )
            assert preview.status_code == 200
            assert preview.json()["removed"][0]["stamp"] == "20260801-000000"

            removed = await client.delete("/api/admin/backups/20260801-000000")
            assert removed.status_code == 200
            assert removed.json()["deleted"]["freed_bytes"] == 200

            gone = await client.delete("/api/admin/backups/20260801-000000")
            assert gone.status_code == 404

            invalid = await client.delete("/api/admin/backups/..%2Fescape")
            assert invalid.status_code == 422

            cleanup = await client.post("/api/admin/backups/cleanup", json={"keep": 1, "dry_run": False})
            assert cleanup.status_code == 200
            assert cleanup.json()["removed"] == []
            assert (tmp_path / "code-20260802-000000").exists()
    finally:
        app.dependency_overrides.clear()

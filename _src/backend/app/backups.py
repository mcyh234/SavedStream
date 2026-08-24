from __future__ import annotations

import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Backup layout created by deploy.ps1 on the server:
#
#   /opt/tube/backups/
#     code-<stamp>/     source tree of the previous deployment
#       _src/
#       TeleBox/
#     volumes-<stamp>/  data volume archives of the previous deployment
#       savedstream-data.tgz
#       telebox-data.tgz
#       caddy-data.tgz
#       caddy-config.tgz
#
# The <stamp> is shared by both directories so one deployment equals one
# logical backup.

_STAMP_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CODE_PREFIX = "code-"
_VOLUME_PREFIX = "volumes-"
_LIST_CACHE_SECONDS = 10
_list_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _iso_from_mtime(value: float) -> str:
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _directory_size(path: Path) -> tuple[int, int]:
    """Return (total bytes, file count) for a directory tree."""
    total = 0
    files = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(Path(root) / name)
                files += 1
            except OSError:
                continue
    return total, files


def _entries_of(path: Path) -> list[str]:
    try:
        return sorted(
            str(entry.name) for entry in path.iterdir() if entry.is_file()
        )
    except OSError:
        return []


def _deletable(path: Path) -> bool:
    """Whether the container UID may remove entries inside this directory.

    deploy.ps1 chowns the backups directory to UID 10001 (the container
    user).  Older installs leave it owned by root, in which case deletion is
    refused with a clear message instead of failing mid-way.
    """
    try:
        return os.access(path, os.W_OK)
    except OSError:
        return False


def list_backups(backups_dir: Path) -> dict[str, Any]:
    """Aggregate code/volumes backup pairs into per-deployment entries."""
    if not backups_dir.is_dir():
        return {"configured": False, "writable": False, "items": []}
    now = time.monotonic()
    cached = _list_cache.get(str(backups_dir))
    if cached and now - cached[0] < _LIST_CACHE_SECONDS:
        return cached[1]

    code_dirs: dict[str, Path] = {}
    volume_dirs: dict[str, Path] = {}
    try:
        for entry in backups_dir.iterdir():
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith(_CODE_PREFIX):
                stamp = name[len(_CODE_PREFIX):]
                if _STAMP_PATTERN.match(stamp):
                    code_dirs[stamp] = entry
            elif name.startswith(_VOLUME_PREFIX):
                stamp = name[len(_VOLUME_PREFIX):]
                if _STAMP_PATTERN.match(stamp):
                    volume_dirs[stamp] = entry
    except OSError:
        return {"configured": True, "writable": _deletable(backups_dir), "items": []}

    stamps = sorted(set(code_dirs) | set(volume_dirs), reverse=True)
    items: list[dict[str, Any]] = []
    for stamp in stamps:
        code_path = code_dirs.get(stamp)
        volume_path = volume_dirs.get(stamp)
        code_size = code_files = 0
        volume_size = volume_files = 0
        modified = 0.0
        if code_path:
            code_size, code_files = _directory_size(code_path)
            modified = max(modified, code_path.stat().st_mtime)
        if volume_path:
            volume_size, volume_files = _directory_size(volume_path)
            modified = max(modified, volume_path.stat().st_mtime)
        items.append(
            {
                "stamp": stamp,
                "size_bytes": code_size + volume_size,
                "file_count": code_files + volume_files,
                "modified_at": _iso_from_mtime(modified),
                "code_size_bytes": code_size,
                "volume_size_bytes": volume_size,
                "code_files": _entries_of(code_path) if code_path else [],
                "volume_files": _entries_of(volume_path) if volume_path else [],
                "has_code": code_path is not None,
                "has_volumes": volume_path is not None,
                "deletable": _deletable(backups_dir),
            }
        )

    payload = {
        "configured": True,
        "writable": _deletable(backups_dir),
        "dir": str(backups_dir),
        "items": items,
    }
    _list_cache[str(backups_dir)] = (now, payload)
    return payload


def _resolve_backup_dir(backups_dir: Path, stamp: str) -> tuple[Path, Path]:
    if not _STAMP_PATTERN.match(stamp):
        raise ValueError("invalid backup stamp")
    root = backups_dir.resolve()
    code = (root / f"{_CODE_PREFIX}{stamp}").resolve()
    volumes = (root / f"{_VOLUME_PREFIX}{stamp}").resolve()
    for candidate in (code, volumes):
        if not str(candidate).startswith(str(root) + os.sep):
            raise ValueError("backup path escapes the backups directory")
    return code, volumes


def delete_backup(backups_dir: Path, stamp: str) -> dict[str, Any]:
    """Delete one deployment backup (code + volume directories)."""
    try:
        code, volumes = _resolve_backup_dir(backups_dir, stamp)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    removed: list[str] = []
    freed = 0
    for path in (code, volumes):
        if path.is_dir():
            size, _count = _directory_size(path)
            shutil.rmtree(path, ignore_errors=False)
            freed += size
            removed.append(path.name)
    if not removed:
        raise FileNotFoundError("backup not found")
    _list_cache.pop(str(backups_dir), None)
    return {"stamp": stamp, "removed": removed, "freed_bytes": freed}


def cleanup_backups(
    backups_dir: Path,
    keep: int,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Keep the most recent N backups and remove everything older."""
    keep = max(1, min(20, int(keep)))
    listing = list_backups(backups_dir)
    if not listing.get("configured") or not listing.get("writable"):
        return {
            "dry_run": dry_run,
            "removed": [],
            "freed_bytes": 0,
            "kept": 0,
            "skipped_reason": "备份目录不存在或容器没有写权限" if not listing.get("configured") else "备份目录当前不可写（宿主目录属主不是容器用户）",
        }
    items = listing["items"]
    items.sort(key=lambda item: (item["modified_at"], item["stamp"]), reverse=True)
    stale = items[keep:]
    removed: list[dict[str, Any]] = []
    freed = 0
    for item in stale:
        if dry_run:
            removed.append(item)
            freed += int(item["size_bytes"] or 0)
            continue
        try:
            result = delete_backup(backups_dir, str(item["stamp"]))
            removed.append({**item, "freed_bytes": result["freed_bytes"]})
            freed += result["freed_bytes"]
        except (ValueError, FileNotFoundError, OSError):
            continue
    return {
        "dry_run": dry_run,
        "removed": removed,
        "freed_bytes": freed,
        "kept": len(items[:keep]),
    }

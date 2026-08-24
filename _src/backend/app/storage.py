from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from .config import settings

# Alert thresholds -----------------------------------------------------------
LOW_SPACE_PERCENT = 10          # warning below 10% free
LOW_SPACE_MIN_BYTES = 10 * 1000**3
CRITICAL_SPACE_PERCENT = 5      # critical below 5% free
CRITICAL_MIN_BYTES = 5 * 1000**3
BACKUP_WARN_BYTES = 20 * 1000**3
BACKUP_WARN_COUNT = 6
CACHE_WARN_PERCENT = 80         # relative to the configured cache limit
CACHE_WARN_MIN_BYTES = 10 * 1000**3
WATCHDOG_INTERVAL_SECONDS = 6 * 60 * 60
WATCHDOG_INITIAL_DELAY_SECONDS = 60

_ALERT_STATE_KEY = "storage_alert_state"


def evaluate_storage_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Turn raw storage metrics into alerts and actionable recommendations.

    Kept as a pure function so the alert rules are unit-testable without a
    real disk.
    """
    alerts: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []

    total = max(1, int(metrics.get("host_total_bytes") or 0))
    free = int(metrics.get("host_free_bytes") or 0)
    free_percent = free / total * 100

    if free_percent < CRITICAL_SPACE_PERCENT or free < CRITICAL_MIN_BYTES:
        alerts.append(
            {
                "level": "critical",
                "code": "LOW_SPACE_CRITICAL",
                "title": "服务器磁盘空间严重不足",
                "message": f"剩余 {format_gigabytes(free)}（{free_percent:.1f}%），建议立即清理缓存或旧部署备份。",
            }
        )
        recommendations.append(
            {
                "code": "cleanup_backups",
                "action": "cleanup_backups",
                "title": "立即清理旧部署备份",
                "message": "磁盘剩余不足，删除历史备份可释放空间（保留最近 3 份即可）。",
            }
        )
        recommendations.append(
            {
                "code": "clear_cache",
                "action": "clear_cache",
                "title": "清空媒体缓存",
                "message": "清空按需缓存分块，正在播放的媒体可能需要重新拉取。",
            }
        )
    elif free_percent < LOW_SPACE_PERCENT or free < LOW_SPACE_MIN_BYTES:
        alerts.append(
            {
                "level": "warning",
                "code": "LOW_SPACE",
                "title": "服务器磁盘空间偏低",
                "message": f"剩余 {format_gigabytes(free)}（{free_percent:.1f}%），建议清理旧备份或调整缓存上限。",
            }
        )
        recommendations.append(
            {
                "code": "cleanup_backups",
                "action": "cleanup_backups",
                "title": "清理旧部署备份",
                "message": "历史备份占用可观空间，删除旧备份可释放磁盘。",
            }
        )

    backup_bytes = int(metrics.get("backups_bytes") or 0)
    backup_count = int(metrics.get("backup_count") or 0)
    if backup_bytes > BACKUP_WARN_BYTES or backup_count > BACKUP_WARN_COUNT:
        level = "warning"
        if free_percent < LOW_SPACE_PERCENT:
            level = "critical"
        alerts.append(
            {
                "level": level,
                "code": "BACKUPS_LARGE",
                "title": "历史部署备份占用较多",
                "message": f"备份共 {backup_count} 份，占用 {format_gigabytes(backup_bytes)}，建议执行保留策略。",
            }
        )
        recommendations.append(
            {
                "code": "cleanup_backups",
                "action": "cleanup_backups",
                "title": "执行备份保留策略",
                "message": f"保留最近 3 份备份，可释放约 {format_gigabytes(backup_bytes)}（按当前占用估算）。",
            }
        )

    cache_bytes = int(metrics.get("cache_bytes") or 0)
    cache_limit = int(metrics.get("cache_limit_bytes") or 0)
    cache_percent = cache_bytes / cache_limit * 100 if cache_limit > 0 else 0
    if cache_bytes > CACHE_WARN_MIN_BYTES and cache_percent >= CACHE_WARN_PERCENT:
        alerts.append(
            {
                "level": "warning",
                "code": "CACHE_FULL",
                "title": "媒体缓存接近上限",
                "message": f"缓存占用 {format_gigabytes(cache_bytes)}（上限的 {cache_percent:.0f}%）。",
            }
        )
        recommendations.append(
            {
                "code": "clear_cache",
                "action": "clear_cache",
                "title": "清空或调低媒体缓存",
                "message": "清空缓存可立即释放磁盘；也可以在缓存页调低上限。",
            }
        )

    return {"alerts": alerts, "recommendations": recommendations}


def format_gigabytes(value: int) -> str:
    return f"{value / 1000**3:.1f} GB"


def _disk_usage(path: Path) -> dict[str, int]:
    try:
        usage = shutil.disk_usage(path)
        return {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "percent_used": round(usage.used / max(1, usage.total) * 100, 1),
        }
    except OSError:
        return {"total_bytes": 0, "used_bytes": 0, "free_bytes": 0, "percent_used": 0.0}


def storage_snapshot(
    *,
    cache_bytes: int,
    cache_files: int,
    cache_limit_bytes: int,
    database_bytes: int,
    backups: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect a full storage snapshot for the admin console."""
    probe = settings.backups_dir if settings.backups_dir.is_dir() else settings.data_dir
    host = _disk_usage(probe)
    data_volume = _disk_usage(settings.data_dir)

    backup_items = (backups or {}).get("items") or []
    backups_bytes = sum(int(item.get("size_bytes") or 0) for item in backup_items)
    backup_count = len(backup_items)
    writable = bool((backups or {}).get("writable"))

    metrics = {
        "host_total_bytes": host["total_bytes"],
        "host_free_bytes": host["free_bytes"],
        "backups_bytes": backups_bytes,
        "backup_count": backup_count,
        "cache_bytes": cache_bytes,
        "cache_limit_bytes": cache_limit_bytes,
    }
    evaluation = evaluate_storage_metrics(metrics)

    return {
        "host": host,
        "data_volume": data_volume,
        "data_volume_path": str(settings.data_dir),
        "backups": {
            "bytes": backups_bytes,
            "count": backup_count,
            "writable": writable,
            "configured": bool((backups or {}).get("configured")),
        },
        "cache": {
            "bytes": cache_bytes,
            "files": cache_files,
            "limit_bytes": cache_limit_bytes,
            "percent_used": round(cache_bytes / max(1, cache_limit_bytes) * 100, 1),
        },
        "database_bytes": database_bytes,
        "probe_path": str(probe),
        **evaluation,
    }


def _host_free_bytes() -> int:
    probe = settings.backups_dir if settings.backups_dir.is_dir() else settings.data_dir
    return _disk_usage(probe)["free_bytes"]


async def _notify_admins(database: Any, title: str, body: str) -> int:
    import aiosqlite

    async with aiosqlite.connect(database.path) as db:
        cursor = await db.execute(
            "SELECT id FROM auth_users WHERE role IN ('admin','superadmin') AND status='approved'"
        )
        rows = await cursor.fetchall()
    sent = 0
    for row in rows:
        if await database.create_notification(int(row[0]), "system", title, body):
            sent += 1
    return sent


async def check_storage_alerts(database: Any) -> dict[str, Any]:
    """Compare the current alert level with the last notified one and send
    a mailbox notification when the level changes."""
    free = _host_free_bytes()
    probe_total = max(1, _disk_usage(settings.backups_dir if settings.backups_dir.is_dir() else settings.data_dir)["total_bytes"])
    free_percent = free / probe_total * 100
    level = "ok"
    if free_percent < CRITICAL_SPACE_PERCENT or free < CRITICAL_MIN_BYTES:
        level = "critical"
    elif free_percent < LOW_SPACE_PERCENT or free < LOW_SPACE_MIN_BYTES:
        level = "warning"

    previous = await database.get_setting(_ALERT_STATE_KEY, "ok")
    result = {"level": level, "previous": previous, "notified": False}
    if level == previous:
        return result
    await database.set_setting(_ALERT_STATE_KEY, level)
    if level == "ok":
        result["notified"] = bool(
            await _notify_admins(database, "存储空间已恢复", f"服务器磁盘剩余空间已恢复到 {free_percent:.1f}%。")
        )
    elif level == "warning":
        result["notified"] = bool(
            await _notify_admins(
                database,
                "存储空间告警",
                f"服务器磁盘剩余空间偏低：{format_gigabytes(free)}（{free_percent:.1f}%）。请清理旧备份或缓存。",
            )
        )
    else:
        result["notified"] = bool(
            await _notify_admins(
                database,
                "存储空间严重不足",
                f"服务器磁盘剩余空间仅 {format_gigabytes(free)}（{free_percent:.1f}%），请立即清理。",
            )
        )
    return result


async def storage_watchdog(
    database: Any,
    *,
    interval: int = WATCHDOG_INTERVAL_SECONDS,
    initial_delay: int = WATCHDOG_INITIAL_DELAY_SECONDS,
) -> None:
    """Background loop that alerts administrators when free space degrades."""
    await asyncio.sleep(max(0, initial_delay))
    while True:
        try:
            await check_storage_alerts(database)
        except Exception:
            # Never let a storage check take down the application; the next
            # cycle retries.
            pass
        await asyncio.sleep(max(60, interval))

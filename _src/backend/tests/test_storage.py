from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app import storage as storage_module
from app.storage import evaluate_storage_metrics, format_gigabytes, storage_snapshot


def base_metrics(**overrides) -> dict:
    metrics = {
        "host_total_bytes": 1000 * 1000**3,   # 1000 GB
        "host_free_bytes": 500 * 1000**3,     # 500 GB
        "backups_bytes": 5 * 1000**3,
        "backup_count": 2,
        "cache_bytes": 5 * 1000**3,
        "cache_limit_bytes": 20 * 1000**3,
    }
    metrics.update(overrides)
    return metrics


def test_healthy_storage_has_no_alerts_or_suggestions() -> None:
    result = evaluate_storage_metrics(base_metrics())
    assert result["alerts"] == []
    assert result["recommendations"] == []


def test_low_space_triggers_warning_and_suggestions() -> None:
    result = evaluate_storage_metrics(base_metrics(host_free_bytes=8 * 1000**3))
    codes = [alert["code"] for alert in result["alerts"]]
    assert "LOW_SPACE" in codes
    actions = {entry["action"] for entry in result["recommendations"]}
    assert {"cleanup_backups", "clear_cache"} <= actions


def test_critical_space_triggers_critical_alert() -> None:
    result = evaluate_storage_metrics(base_metrics(host_free_bytes=3 * 1000**3))
    assert result["alerts"][0]["code"] == "LOW_SPACE_CRITICAL"
    assert result["alerts"][0]["level"] == "critical"


def test_large_backups_trigger_backup_alert() -> None:
    result = evaluate_storage_metrics(
        base_metrics(backups_bytes=30 * 1000**3, backup_count=7)
    )
    codes = [alert["code"] for alert in result["alerts"]]
    assert "BACKUPS_LARGE" in codes
    assert any(entry["action"] == "cleanup_backups" for entry in result["recommendations"])


def test_full_cache_triggers_cache_alert() -> None:
    result = evaluate_storage_metrics(
        base_metrics(cache_bytes=18 * 1000**3, cache_limit_bytes=20 * 1000**3)
    )
    codes = [alert["code"] for alert in result["alerts"]]
    assert "CACHE_FULL" in codes
    assert any(entry["action"] == "clear_cache" for entry in result["recommendations"])


def test_snapshot_shape_and_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = tmp_path / "probe"
    data = tmp_path / "data"
    probe.mkdir()
    data.mkdir()
    monkeypatch.setattr(
        storage_module,
        "settings",
        replace(storage_module.settings, backups_dir=probe, data_dir=data),
    )
    snapshot = storage_snapshot(
        cache_bytes=1000,
        cache_files=2,
        cache_limit_bytes=10 * 1000**3,
        database_bytes=4096,
        backups={"configured": True, "writable": True, "items": []},
    )
    assert snapshot["host"]["total_bytes"] > 0
    assert snapshot["data_volume"]["total_bytes"] > 0
    assert snapshot["cache"] == {
        "bytes": 1000,
        "files": 2,
        "limit_bytes": 10 * 1000**3,
        "percent_used": 0.0,
    }
    assert snapshot["database_bytes"] == 4096
    assert snapshot["backups"]["count"] == 0
    assert snapshot["probe_path"] != ""
    assert isinstance(snapshot["alerts"], list)
    assert isinstance(snapshot["recommendations"], list)


def test_format_gigabytes() -> None:
    assert format_gigabytes(2 * 1000**3) == "2.0 GB"
    assert format_gigabytes(0) == "0.0 GB"

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.database import Database
from app.traffic import TrafficController, TrafficLimitExceeded


@pytest.mark.asyncio
async def test_monthly_traffic_cap_counts_inbound_and_outbound_together(tmp_path: Path) -> None:
    database = Database(tmp_path / "traffic.db")
    await database.initialize()
    await database.set_traffic_settings(
        enabled=True,
        monthly_capacity_bytes=1_000,
        monthly_limit_bytes=900,
        warning_percent=80,
        admin_bypass=False,
    )

    first = await database.consume_traffic("out", 500, request_count=1)
    second = await database.consume_traffic("in", 400)
    rejected = await database.consume_traffic("out", 1)

    assert first["allowed"] and second["allowed"]
    assert second["used_bytes"] == 900
    assert not rejected["allowed"]
    usage = await database.get_traffic_usage("month")
    assert usage["bytes_in"] == 400
    assert usage["bytes_out"] == 500
    assert usage["request_count"] == 1


@pytest.mark.asyncio
async def test_concurrent_consumers_cannot_pass_the_hard_cap(tmp_path: Path) -> None:
    database = Database(tmp_path / "traffic-concurrent.db")
    await database.initialize()
    await database.set_traffic_settings(
        enabled=True,
        monthly_capacity_bytes=100,
        monthly_limit_bytes=100,
        warning_percent=80,
        admin_bypass=False,
    )

    results = await asyncio.gather(
        *(database.consume_traffic("out", 60) for _ in range(3))
    )
    assert sum(1 for result in results if result["allowed"]) == 1
    assert (await database.get_traffic_usage("month"))["bytes_out"] == 60


@pytest.mark.asyncio
async def test_series_has_zero_filled_days_and_reset_is_scoped(tmp_path: Path) -> None:
    database = Database(tmp_path / "traffic-series.db")
    await database.initialize()
    await database.consume_traffic("out", 12)
    series = await database.list_traffic_series("7d")
    assert len(series) == 7
    assert series[-1]["bytes_out"] == 12
    assert sum(item["bytes_total"] for item in series) == 12

    await database.reset_traffic_usage("month")
    assert (await database.get_traffic_usage("month"))["bytes_out"] == 0
    assert all(item["bytes_total"] == 0 for item in await database.list_traffic_series("7d"))


@pytest.mark.asyncio
async def test_controller_raises_limit_error_and_tracks_live_rate(tmp_path: Path) -> None:
    database = Database(tmp_path / "traffic-controller.db")
    await database.initialize()
    await database.set_traffic_settings(
        enabled=True,
        monthly_capacity_bytes=1_000,
        monthly_limit_bytes=600,
        warning_percent=80,
        admin_bypass=False,
    )
    controller = TrafficController(database)
    await controller.start_request("stream", "out")
    try:
        await controller.consume("out", 600)
        with pytest.raises(TrafficLimitExceeded):
            await controller.consume("out", 1)
        snapshot = await controller.snapshot()
        assert snapshot.active_requests == 1
        assert snapshot.active_streams == 1
        assert snapshot.outbound_bps > 0
    finally:
        await controller.finish_request("stream")

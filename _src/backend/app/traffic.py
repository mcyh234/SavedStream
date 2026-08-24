from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from .database import Database


class TrafficLimitExceeded(Exception):
    def __init__(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        super().__init__("Monthly traffic limit reached")


@dataclass(frozen=True)
class TrafficSnapshot:
    active_requests: int
    active_streams: int
    active_uploads: int
    inbound_bps: int
    outbound_bps: int


class TrafficController:
    """Process-local activity telemetry backed by durable SQLite counters."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._lock = asyncio.Lock()
        self._active_requests = 0
        self._active_streams = 0
        self._active_uploads = 0
        self._events: deque[tuple[float, str, int]] = deque()

    async def start_request(self, kind: str, direction: str) -> None:
        if kind not in {"stream", "upload", "request"}:
            kind = "request"
        await self.database.consume_traffic(direction, 0, request_count=1)
        async with self._lock:
            self._active_requests += 1
            if kind == "stream":
                self._active_streams += 1
            elif kind == "upload":
                self._active_uploads += 1

    async def finish_request(self, kind: str) -> None:
        async with self._lock:
            self._active_requests = max(0, self._active_requests - 1)
            if kind == "stream":
                self._active_streams = max(0, self._active_streams - 1)
            elif kind == "upload":
                self._active_uploads = max(0, self._active_uploads - 1)

    async def ensure_available(self, amount: int, *, bypass_limit: bool = False) -> dict:
        result = await self.database.consume_traffic("out", 0, bypass_limit=bypass_limit)
        if result["enabled"] and int(amount) > int(result["remaining_bytes"]):
            raise TrafficLimitExceeded(result)
        return result

    async def consume(
        self,
        direction: str,
        amount: int,
        *,
        bypass_limit: bool = False,
    ) -> dict:
        result = await self.database.consume_traffic(
            direction,
            amount,
            bypass_limit=bypass_limit,
        )
        if not result["allowed"]:
            raise TrafficLimitExceeded(result)
        if amount:
            async with self._lock:
                self._events.append((time.monotonic(), direction, int(amount)))
                self._prune_events(time.monotonic())
        return result

    async def snapshot(self) -> TrafficSnapshot:
        now = time.monotonic()
        async with self._lock:
            self._prune_events(now)
            inbound = sum(amount for _, direction, amount in self._events if direction == "in")
            outbound = sum(amount for _, direction, amount in self._events if direction == "out")
            return TrafficSnapshot(
                active_requests=self._active_requests,
                active_streams=self._active_streams,
                active_uploads=self._active_uploads,
                inbound_bps=round(inbound / 60),
                outbound_bps=round(outbound / 60),
            )

    def _prune_events(self, now: float) -> None:
        cutoff = now - 60
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    @asynccontextmanager
    async def request(self, kind: str, direction: str) -> AsyncIterator[None]:
        await self.start_request(kind, direction)
        try:
            yield
        finally:
            await self.finish_request(kind)

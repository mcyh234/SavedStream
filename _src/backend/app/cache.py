from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from .media_crypto import CacheCipher, CacheDecryptionError


class DiskCache:
    def __init__(
        self,
        root: Path,
        limit_provider: Callable[[], Awaitable[int]],
        encryption_key: str,
    ) -> None:
        self.root = root
        self._limit_provider = limit_provider
        self._cipher = CacheCipher(encryption_key)
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._locks_guard = asyncio.Lock()
        self._eviction_lock = asyncio.Lock()
        self._generation = 0

    async def initialize(self) -> None:
        await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)

    async def get_chunk(
        self,
        media_key: str,
        chunk_index: int,
        expected_size: int,
        loader: Callable[[], Awaitable[bytes]],
    ) -> bytes:
        path = self.root / "chunks" / self._key_digest(media_key) / f"{chunk_index}.bin"
        return await self._get_or_create(
            path, media_key, loader, expected_size=expected_size
        )

    async def get_thumbnail(
        self, media_key: str, loader: Callable[[], Awaitable[bytes]]
    ) -> bytes:
        path = self.root / "thumbnails" / f"{self._key_digest(media_key)}.img"
        return await self._get_or_create(path, media_key, loader)

    async def get_cached_thumbnail(self, media_key: str) -> bytes | None:
        path = self.root / "thumbnails" / f"{self._key_digest(media_key)}.img"
        return await asyncio.to_thread(self._read_cached, path, media_key, None)

    async def _get_or_create(
        self,
        path: Path,
        media_key: str,
        loader: Callable[[], Awaitable[bytes]],
        *,
        expected_size: int | None = None,
    ) -> bytes:
        lock = await self._lock_for(str(path))
        try:
            async with lock:
                cached = await asyncio.to_thread(
                    self._read_cached, path, media_key, expected_size
                )
                if cached is not None:
                    return cached

                generation = self._generation
                data = await loader()
                if not data:
                    return data
                if expected_size is not None and len(data) != expected_size:
                    raise ValueError(
                        f"Cache loader returned {len(data)} bytes, expected {expected_size}"
                    )

                async with self._eviction_lock:
                    if generation == self._generation:
                        await asyncio.to_thread(
                            self._atomic_write, path, media_key, data
                        )
                await self.evict_if_needed()
                return data
        finally:
            await self._release_lock(str(path), lock)

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            entry = self._locks.get(key)
            if entry is None:
                lock = asyncio.Lock()
                users = 0
            else:
                lock, users = entry
            self._locks[key] = (lock, users + 1)
            return lock

    async def _release_lock(self, key: str, lock: asyncio.Lock) -> None:
        async with self._locks_guard:
            entry = self._locks.get(key)
            if entry is None or entry[0] is not lock:
                return
            users = entry[1] - 1
            if users <= 0:
                self._locks.pop(key, None)
            else:
                self._locks[key] = (lock, users)

    async def stats(self) -> dict[str, int]:
        return await asyncio.to_thread(self._stats_sync)

    async def clear(self) -> None:
        async with self._eviction_lock:
            self._generation += 1
            await asyncio.to_thread(self._clear_sync)

    async def delete_media(self, media_key: str) -> None:
        """Remove every cached representation for one media cache key.

        A media key is shared by the encrypted Telegram chunks and the
        thumbnail.  Deletion is deliberately performed under the eviction
        lock so an in-flight eviction cannot race with the tombstone path.
        """
        async with self._eviction_lock:
            self._generation += 1
            await asyncio.to_thread(self._delete_media_sync, media_key)

    async def evict_if_needed(self) -> None:
        async with self._eviction_lock:
            limit = await self._limit_provider()
            await asyncio.to_thread(self._evict_sync, limit)

    def _read_cached(
        self, path: Path, media_key: str, expected_size: int | None
    ) -> bytes | None:
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            data = self._cipher.decrypt(payload, media_key.encode("utf-8"))
        except CacheDecryptionError:
            path.unlink(missing_ok=True)
            return None
        if expected_size is not None and len(data) != expected_size:
            path.unlink(missing_ok=True)
            return None
        try:
            now = time.time()
            os.utime(path, (now, now))
        except FileNotFoundError:
            pass
        return data

    @staticmethod
    def _key_digest(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _atomic_write(self, path: Path, media_key: str, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = self._cipher.encrypt(data, media_key.encode("utf-8"))
        handle, temporary_name = tempfile.mkstemp(prefix=".part-", dir=path.parent)
        try:
            with os.fdopen(handle, "wb") as temporary:
                temporary.write(encrypted)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _stats_sync(self) -> dict[str, int]:
        total_bytes = 0
        file_count = 0
        if self.root.exists():
            for path in self.root.rglob("*"):
                if path.is_file() and not path.name.startswith(".part-"):
                    try:
                        total_bytes += path.stat().st_size
                        file_count += 1
                    except FileNotFoundError:
                        continue
        return {"bytes": total_bytes, "files": file_count}

    def _evict_sync(self, limit: int) -> None:
        if not self.root.exists():
            return
        files: list[tuple[float, int, Path]] = []
        total = 0
        for path in self.root.rglob("*"):
            if not path.is_file() or path.name.startswith(".part-"):
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            files.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size

        if total <= limit:
            return
        for _, size, path in sorted(files, key=lambda entry: entry[0]):
            try:
                path.unlink()
                total -= size
            except FileNotFoundError:
                pass
            if total <= limit:
                break

    def _clear_sync(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _delete_media_sync(self, media_key: str) -> None:
        digest = self._key_digest(media_key)
        chunk_dir = self.root / "chunks" / digest
        thumbnail = self.root / "thumbnails" / f"{digest}.img"
        if chunk_dir.exists():
            shutil.rmtree(chunk_dir, ignore_errors=True)
        thumbnail.unlink(missing_ok=True)

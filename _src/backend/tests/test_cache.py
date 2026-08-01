import asyncio

import pytest

from app.cache import DiskCache


TEST_CACHE_KEY = "00" * 32


@pytest.mark.asyncio
async def test_concurrent_chunk_requests_share_one_loader(tmp_path):
    async def limit():
        return 1024 * 1024

    cache = DiskCache(tmp_path, limit, TEST_CACHE_KEY)
    await cache.initialize()
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return b"abcd"

    values = await asyncio.gather(
        *(cache.get_chunk("media-v1", 0, 4, loader) for _ in range(8))
    )
    assert values == [b"abcd"] * 8
    assert calls == 1
    assert cache._locks == {}


@pytest.mark.asyncio
async def test_corrupt_chunk_is_replaced(tmp_path):
    async def limit():
        return 1024 * 1024

    cache = DiskCache(tmp_path, limit, TEST_CACHE_KEY)
    await cache.initialize()
    assert await cache.get_chunk("media-v1", 0, 4, lambda: _value(b"abcd")) == b"abcd"

    chunk = next((tmp_path / "chunks").rglob("0.bin"))
    chunk.write_bytes(b"x")
    assert await cache.get_chunk("media-v1", 0, 4, lambda: _value(b"wxyz")) == b"wxyz"


@pytest.mark.asyncio
async def test_wrong_sized_loader_result_is_not_cached(tmp_path):
    async def limit():
        return 1024 * 1024

    cache = DiskCache(tmp_path, limit, TEST_CACHE_KEY)
    await cache.initialize()

    with pytest.raises(ValueError):
        await cache.get_chunk("media-v1", 0, 4, lambda: _value(b"bad"))

    assert list((tmp_path / "chunks").rglob("*.bin")) == []


@pytest.mark.asyncio
async def test_lru_eviction_keeps_cache_under_limit(tmp_path):
    async def limit():
        return 60

    cache = DiskCache(tmp_path, limit, TEST_CACHE_KEY)
    await cache.initialize()
    await cache.get_chunk("older", 0, 4, lambda: _value(b"aaaa"))
    await asyncio.sleep(0.02)
    await cache.get_chunk("newer", 0, 4, lambda: _value(b"bbbb"))

    stats = await cache.stats()
    assert stats["files"] == 1
    assert stats["bytes"] > 4


@pytest.mark.asyncio
async def test_clear_removes_chunks_and_thumbnails(tmp_path):
    async def limit():
        return 1024 * 1024

    cache = DiskCache(tmp_path, limit, TEST_CACHE_KEY)
    await cache.initialize()
    await cache.get_chunk("media-v1", 0, 4, lambda: _value(b"abcd"))
    await cache.get_thumbnail("media-v1", lambda: _value(b"image"))
    stored = next((tmp_path / "thumbnails").glob("*.img")).read_bytes()
    assert b"image" not in stored

    await cache.clear()

    assert await cache.stats() == {"bytes": 0, "files": 0}
    assert tmp_path.is_dir()


@pytest.mark.asyncio
async def test_cached_thumbnail_can_be_read_without_loader(tmp_path):
    async def limit():
        return 1024 * 1024

    cache = DiskCache(tmp_path, limit, TEST_CACHE_KEY)
    await cache.initialize()
    await cache.get_thumbnail("media-v1", lambda: _value(b"image"))

    assert await cache.get_cached_thumbnail("media-v1") == b"image"


async def _value(value):
    return value

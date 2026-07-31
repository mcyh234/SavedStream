from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


DEFAULT_CACHE_BYTES = 20 * 1024 * 1024 * 1024


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS media_metadata (
                    message_id INTEGER PRIMARY KEY,
                    local_title TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS media_metadata_v2 (
                    account_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    local_title TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, message_id)
                );

                INSERT OR IGNORE INTO media_metadata_v2(account_id, message_id, local_title, updated_at)
                SELECT 'default', message_id, local_title, updated_at FROM media_metadata;
                """
            )
            await db.executemany(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                [
                    ("cache_max_bytes", str(DEFAULT_CACHE_BYTES)),
                    ("access_restricted", "0"),
                    ("viewer_key_hash", ""),
                ],
            )
            await db.commit()

    async def get_setting(self, key: str, default: str = "") -> str:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return str(row[0]) if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await db.commit()

    async def update_access_settings(
        self, *, cache_max_bytes: int, access_restricted: bool, viewer_key_hash: str
    ) -> None:
        values = [
            ("cache_max_bytes", str(cache_max_bytes)),
            ("access_restricted", "1" if access_restricted else "0"),
            ("viewer_key_hash", viewer_key_hash),
        ]
        async with aiosqlite.connect(self.path) as db:
            await db.executemany(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                values,
            )
            await db.commit()

    async def get_cache_limit(self) -> int:
        raw = await self.get_setting("cache_max_bytes", str(DEFAULT_CACHE_BYTES))
        try:
            return max(512 * 1024 * 1024, int(raw))
        except ValueError:
            return DEFAULT_CACHE_BYTES

    async def access_restricted(self) -> bool:
        return await self.get_setting("access_restricted", "0") == "1"

    async def get_local_titles(self, message_ids: list[int], account_id: str = "default") -> dict[int, str]:
        if not message_ids:
            return {}
        placeholders = ",".join("?" for _ in message_ids)
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                f"SELECT message_id, local_title FROM media_metadata_v2 WHERE account_id = ? AND message_id IN ({placeholders})",
                [account_id, *message_ids],
            )
            rows = await cursor.fetchall()
            return {int(row[0]): str(row[1]) for row in rows}

    async def set_local_title(self, message_id: int, title: str, account_id: str = "default") -> None:
        clean_title = title.strip()
        async with aiosqlite.connect(self.path) as db:
            if clean_title:
                await db.execute(
                    "INSERT INTO media_metadata_v2(account_id, message_id, local_title, updated_at) VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(account_id, message_id) DO UPDATE SET local_title = excluded.local_title, "
                    "updated_at = excluded.updated_at",
                    (account_id, message_id, clean_title, datetime.now(timezone.utc).isoformat()),
                )
            else:
                await db.execute(
                    "DELETE FROM media_metadata_v2 WHERE account_id = ? AND message_id = ?", (account_id, message_id)
                )
            await db.commit()

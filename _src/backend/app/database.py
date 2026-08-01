from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
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

                CREATE TABLE IF NOT EXISTS device_keys (
                    fingerprint TEXT PRIMARY KEY,
                    public_key_pem TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS media_metadata_v2 (
                    account_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    local_title TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS media_users (
                    telegram_user_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    username TEXT,
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'disabled', 'denied')),
                    requested_at TEXT NOT NULL,
                    approved_at TEXT,
                    last_login_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS access_sessions (
                    token_hash TEXT PRIMARY KEY,
                    telegram_user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    FOREIGN KEY(telegram_user_id) REFERENCES media_users(telegram_user_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS access_sessions_user_idx
                ON access_sessions(telegram_user_id);

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

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def upsert_media_user(self, identity: dict[str, str | None]) -> dict[str, str | None]:
        now = datetime.now(timezone.utc).isoformat()
        values = (
            str(identity["telegram_user_id"]),
            str(identity["account_id"]),
            identity.get("username"),
            str(identity.get("display_name") or f"Telegram {identity['telegram_user_id']}"),
            now,
            now,
        )
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT account_id FROM media_users WHERE telegram_user_id=?",
                (values[0],),
            )
            existing = await cursor.fetchone()
            await db.execute(
                "INSERT INTO media_users(telegram_user_id,account_id,username,display_name,status,requested_at,last_login_at) "
                "VALUES(?,?,?,?,'pending',?,?) "
                "ON CONFLICT(telegram_user_id) DO UPDATE SET account_id=excluded.account_id, "
                "username=excluded.username, display_name=excluded.display_name, last_login_at=excluded.last_login_at, "
                "status=CASE WHEN media_users.account_id<>excluded.account_id THEN 'pending' ELSE media_users.status END, "
                "approved_at=CASE WHEN media_users.account_id<>excluded.account_id THEN NULL ELSE media_users.approved_at END",
                values,
            )
            if existing and str(existing[0]) != values[1]:
                await db.execute("DELETE FROM access_sessions WHERE telegram_user_id=?", (values[0],))
            await db.commit()
        record = await self.get_media_user(str(identity["telegram_user_id"]))
        assert record is not None
        return record

    async def get_media_user(self, telegram_user_id: str) -> dict[str, str | None] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT telegram_user_id,account_id,username,display_name,status,requested_at,approved_at,last_login_at "
                "FROM media_users WHERE telegram_user_id=?",
                (telegram_user_id,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_media_users(self) -> list[dict[str, str | None]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT telegram_user_id,account_id,username,display_name,status,requested_at,approved_at,last_login_at "
                "FROM media_users ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, requested_at DESC"
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def set_media_user_status(self, telegram_user_id: str, user_status: str) -> dict[str, str | None] | None:
        if user_status not in {"pending", "approved", "disabled", "denied"}:
            raise ValueError("invalid media user status")
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "UPDATE media_users SET status=?, approved_at=CASE WHEN ?='approved' THEN ? ELSE approved_at END "
                "WHERE telegram_user_id=?",
                (user_status, user_status, now, telegram_user_id),
            )
            if cursor.rowcount == 0:
                return None
            if user_status != "approved":
                await db.execute("DELETE FROM access_sessions WHERE telegram_user_id=?", (telegram_user_id,))
            await db.commit()
        return await self.get_media_user(telegram_user_id)

    async def create_access_session(self, token: str, telegram_user_id: str, ttl_seconds: int) -> None:
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO access_sessions(token_hash,telegram_user_id,created_at,expires_at,last_used_at) VALUES(?,?,?,?,?)",
                (
                    self._token_hash(token),
                    telegram_user_id,
                    now.isoformat(),
                    (now + timedelta(seconds=ttl_seconds)).isoformat(),
                    now.isoformat(),
                ),
            )
            await db.commit()

    async def get_access_session(self, token: str | None) -> dict[str, str | None] | None:
        if not token:
            return None
        token_hash = self._token_hash(token)
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT u.telegram_user_id,u.account_id,u.username,u.display_name,u.status,u.requested_at,u.approved_at,u.last_login_at "
                "FROM access_sessions s JOIN media_users u ON u.telegram_user_id=s.telegram_user_id "
                "WHERE s.token_hash=? AND s.expires_at>?",
                (token_hash, now),
            )
            row = await cursor.fetchone()
            if row:
                await db.execute("UPDATE access_sessions SET last_used_at=? WHERE token_hash=?", (now, token_hash))
            else:
                await db.execute("DELETE FROM access_sessions WHERE token_hash=?", (token_hash,))
            await db.commit()
        return dict(row) if row else None

    async def revoke_access_session(self, token: str | None) -> None:
        if not token:
            return
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM access_sessions WHERE token_hash=?", (self._token_hash(token),))
            await db.commit()


    async def register_device_key(self, fingerprint: str, public_key_pem: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO device_keys(fingerprint, public_key_pem, created_at, last_used_at, revoked) VALUES(?,?,?,?,0) "
                "ON CONFLICT(fingerprint) DO UPDATE SET public_key_pem=excluded.public_key_pem, last_used_at=excluded.last_used_at",
                (fingerprint, public_key_pem, now, now),
            )
            cursor = await db.execute("SELECT revoked FROM device_keys WHERE fingerprint=?", (fingerprint,))
            row = await cursor.fetchone()
            await db.commit()
        return bool(row and not int(row[0]))

    async def get_device_key(self, fingerprint: str) -> dict[str, str | int] | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT fingerprint, public_key_pem, created_at, last_used_at, revoked FROM device_keys WHERE fingerprint=?",
                (fingerprint,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        return {"fingerprint": str(row[0]), "public_key_pem": str(row[1]), "created_at": str(row[2]), "last_used_at": str(row[3]), "revoked": int(row[4])}

    async def touch_device_key(self, fingerprint: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE device_keys SET last_used_at=? WHERE fingerprint=?", (now, fingerprint))
            await db.commit()

    async def revoke_device_key(self, fingerprint: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE device_keys SET last_used_at=?, revoked=1 WHERE fingerprint=?", (now, fingerprint))
            await db.commit()

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

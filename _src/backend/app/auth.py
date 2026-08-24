from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
PASSWORD_MIN_LENGTH = 12
CHALLENGE_TTL_SECONDS = 15 * 60
TRUSTED_DEVICE_TTL_SECONDS = 30 * 24 * 60 * 60


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_username(value: str) -> str:
    return value.strip().lower()


def validate_username(value: str) -> str:
    clean = value.strip()
    if not USERNAME_RE.fullmatch(clean):
        raise ValueError("username must be 3-32 ASCII letters, numbers, _, ., or -")
    return normalize_username(clean)


def validate_password(value: str) -> str:
    if len(value) < PASSWORD_MIN_LENGTH or len(value) > 128:
        raise ValueError("password must be 12-128 characters")
    return value


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + salt.hex() + "$" + digest.hex()


def verify_password(password: str, encoded: str | None) -> bool:
    try:
        algorithm, salt_hex, digest_hex = str(encoded or "").split("$", 2)
        if algorithm != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = bytes.fromhex(hash_password(password, bytes.fromhex(salt_hex)).split("$", 2)[2])
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_browser_id(value: str | None) -> str:
    return hash_token((value or "anonymous-browser").strip()[:256])


class AuthStore:
    def __init__(self, path) -> None:
        self.path = path

    async def _row(self, query: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, args)
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def _rows(self, query: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, args)
            return [dict(row) for row in await cursor.fetchall()]

    async def admin_count(self) -> int:
        row = await self._row("SELECT COUNT(*) AS count FROM auth_users WHERE role IN ('admin','superadmin')")
        return int(row["count"] if row else 0)

    async def superadmin_count(self) -> int:
        row = await self._row("SELECT COUNT(*) AS count FROM auth_users WHERE role='superadmin' AND status='approved'")
        return int(row["count"] if row else 0)

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        return await self._row("SELECT * FROM auth_users WHERE id=?", (int(user_id),))

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        return await self._row("SELECT * FROM auth_users WHERE username_normalized=?", (normalize_username(username),))

    async def get_user_by_telegram(self, telegram_user_id: str) -> dict[str, Any] | None:
        return await self._row("SELECT * FROM auth_users WHERE telegram_user_id=?", (str(telegram_user_id),))

    async def list_users(self) -> list[dict[str, Any]]:
        return await self._rows(
            "SELECT id,username_normalized,username_display,password_hash,role,status,"
            "telegram_user_id,telegram_username,display_name,account_id,binding_sync_status,"
            "legacy_claim_required,ban_reason,created_at,approved_at,last_login_at,password_changed_at "
            "FROM auth_users ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END, created_at DESC"
        )

    async def create_admin(self, username: str, password: str, *, role: str = "superadmin") -> dict[str, Any]:
        normalized = validate_username(username)
        validate_password(password)
        if role not in {"admin", "superadmin"}:
            raise ValueError("invalid administrator role")
        now = now_iso()
        password_hash = hash_password(password)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                exists = await (await db.execute("SELECT 1 FROM auth_users WHERE username_normalized=?", (normalized,))).fetchone()
                if exists:
                    raise ValueError("username is already in use")
                cursor = await db.execute(
                    "INSERT INTO auth_users(username_normalized,username_display,password_hash,role,status,"
                    "display_name,binding_sync_status,created_at,approved_at,password_changed_at) "
                    "VALUES(?,?,?,?,'approved',?,'not_required',?,?,?)",
                    (normalized, username.strip(), password_hash, role, username.strip(), now, now, now),
                )
                user_id = int(cursor.lastrowid)
                await db.execute("INSERT INTO auth_audit_events(user_id,action,metadata,created_at) VALUES(?,?,?,?)", (user_id, "admin_bootstrap", "{}", now))
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return (await self.get_user(user_id)) or {}

    async def register_challenge(
        self,
        username: str,
        password: str,
        *,
        kind: str = "register",
        user_id: int | None = None,
        browser_id_hash: str | None = None,
        trust_requested: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        normalized = validate_username(username)
        validate_password(password)
        if kind not in {"register", "legacy_claim"}:
            raise ValueError("invalid registration challenge kind")
        existing = await self.get_user_by_username(normalized)
        if existing and not (kind == "legacy_claim" and int(existing.get("legacy_claim_required") or 0)):
            raise ValueError("username is already in use")
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=CHALLENGE_TTL_SECONDS)).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO auth_challenges(id,token_hash,kind,user_id,username_normalized,username_display,password_hash,"
                "browser_id_hash,trust_requested,status,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?)",
                (token, hash_token(token), kind, user_id, normalized, username.strip(), hash_password(password), browser_id_hash, 1 if trust_requested else 0, expires, now.isoformat()),
            )
            await db.commit()
        return token, {"challenge_id": token, "expires_at": expires, "kind": kind}

    async def create_user_challenge(self, kind: str, user_id: int, *, browser_id_hash: str | None = None, trust_requested: bool = False) -> tuple[str, dict[str, Any]]:
        if kind not in {"device_verify", "password_reset"}:
            raise ValueError("invalid authentication challenge kind")
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=CHALLENGE_TTL_SECONDS)).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO auth_challenges(id,token_hash,kind,user_id,browser_id_hash,trust_requested,status,expires_at,created_at) VALUES(?,?,?,?,?,?, 'pending',?,?)",
                (token, hash_token(token), kind, int(user_id), browser_id_hash, 1 if trust_requested else 0, expires, now.isoformat()),
            )
            await db.commit()
        return token, {"challenge_id": token, "expires_at": expires, "kind": kind}

    async def get_challenge(self, token: str, *, kind: str | None = None) -> dict[str, Any] | None:
        clauses = ["token_hash=?", "status IN ('pending','claimed')", "expires_at>?"]
        args: list[Any] = [hash_token(token), now_iso()]
        if kind:
            clauses.append("kind=?")
            args.append(kind)
        return await self._row("SELECT * FROM auth_challenges WHERE " + " AND ".join(clauses), tuple(args))

    async def claim_challenge(self, token: str, telegram_user_id: str, telegram_username: str | None, display_name: str, *, chat_type: str = "private") -> dict[str, Any]:
        if chat_type != "private":
            raise ValueError("Telegram binding must be completed in a private chat")
        now = now_iso()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute("SELECT * FROM auth_challenges WHERE token_hash=? AND status='pending' AND expires_at>?", (hash_token(token), now))
                challenge_row = await cursor.fetchone()
                if not challenge_row:
                    cursor = await db.execute("SELECT * FROM auth_challenges WHERE token_hash=? AND status='claimed' AND expires_at>?", (hash_token(token), now))
                    challenge_row = await cursor.fetchone()
                    if challenge_row:
                        await db.commit()
                        return dict(challenge_row)
                    raise ValueError("challenge is invalid, expired, or already used")
                challenge = dict(challenge_row)
                kind = str(challenge["kind"])
                existing_cursor = await db.execute("SELECT * FROM auth_users WHERE telegram_user_id=?", (str(telegram_user_id),))
                existing_row = await existing_cursor.fetchone()
                existing = dict(existing_row) if existing_row else None
                user_id = challenge.get("user_id")
                if kind in {"register", "legacy_claim"}:
                    if existing and not int(existing.get("legacy_claim_required") or 0):
                        raise ValueError("Telegram account is already linked")
                    if existing:
                        await db.execute(
                            "UPDATE auth_users SET username_normalized=?,username_display=?,password_hash=?,telegram_username=?,display_name=?,legacy_claim_required=0,last_login_at=? WHERE id=?",
                            (challenge["username_normalized"], challenge["username_display"], challenge["password_hash"], telegram_username, display_name or existing.get("display_name") or "", now, int(existing["id"])),
                        )
                        user_id = int(existing["id"])
                    else:
                        cursor = await db.execute(
                            "INSERT INTO auth_users(username_normalized,username_display,password_hash,role,status,telegram_user_id,telegram_username,display_name,account_id,binding_sync_status,legacy_claim_required,created_at) VALUES(?,?,?,'user','pending',?,?,?,?,?,0,?)",
                            (challenge["username_normalized"], challenge["username_display"], challenge["password_hash"], str(telegram_user_id), telegram_username, display_name or f"Telegram {telegram_user_id}", None, "pending", now),
                        )
                        user_id = int(cursor.lastrowid)
                    await db.execute("UPDATE auth_challenges SET user_id=?,telegram_user_id=?,status='claimed',claimed_at=? WHERE id=?", (int(user_id), str(telegram_user_id), now, challenge["id"]))
                    await db.execute("INSERT INTO auth_audit_events(user_id,action,metadata,created_at) VALUES(?,?,?,?)", (int(user_id), "telegram_bound", "{}", now))
                else:
                    if not user_id or not existing or str(existing.get("telegram_user_id")) != str(telegram_user_id):
                        raise ValueError("Telegram identity does not match this account")
                    await db.execute("UPDATE auth_challenges SET telegram_user_id=?,status='claimed',claimed_at=? WHERE id=?", (str(telegram_user_id), now, challenge["id"]))
                await db.commit()
                return await self.get_challenge(token) or challenge
            except Exception:
                await db.rollback()
                raise

    async def create_session(self, user_id: int, browser_id: str | None, ttl_seconds: int, *, trust_device: bool = False) -> str:
        token = secrets.token_urlsafe(48)
        now = datetime.now(timezone.utc)
        browser_hash = hash_browser_id(browser_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO auth_sessions(token_hash,user_id,browser_id_hash,created_at,expires_at,last_used_at) VALUES(?,?,?,?,?,?)", (hash_token(token), int(user_id), browser_hash, now.isoformat(), (now + timedelta(seconds=ttl_seconds)).isoformat(), now.isoformat()))
            if trust_device:
                await db.execute("INSERT INTO trusted_devices(user_id,browser_id_hash,created_at,expires_at,last_used_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id,browser_id_hash) DO UPDATE SET expires_at=excluded.expires_at,last_used_at=excluded.last_used_at", (int(user_id), browser_hash, now.isoformat(), (now + timedelta(seconds=TRUSTED_DEVICE_TTL_SECONDS)).isoformat(), now.isoformat()))
            await db.execute("UPDATE auth_users SET last_login_at=? WHERE id=?", (now.isoformat(), int(user_id)))
            await db.commit()
        return token

    async def is_trusted_device(self, user_id: int, browser_id: str | None) -> bool:
        row = await self._row("SELECT 1 AS ok FROM trusted_devices WHERE user_id=? AND browser_id_hash=? AND expires_at>?", (int(user_id), hash_browser_id(browser_id), now_iso()))
        return bool(row)

    async def get_session(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        token_hash = hash_token(token)
        now = now_iso()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT u.*,s.browser_id_hash,s.expires_at AS session_expires_at FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>?", (token_hash, now))
            row = await cursor.fetchone()
            if not row:
                await db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash,))
                await db.commit()
                return None
            await db.execute("UPDATE auth_sessions SET last_used_at=? WHERE token_hash=?", (now, token_hash))
            await db.commit()
            return dict(row)

    async def revoke_session(self, token: str | None) -> None:
        if token:
            async with aiosqlite.connect(self.path) as db:
                await db.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=?", (now_iso(), hash_token(token)))
                await db.commit()

    async def revoke_user_sessions(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now_iso(), int(user_id)))
            await db.commit()

    async def revoke_user_devices(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM trusted_devices WHERE user_id=?", (int(user_id),))
            await db.commit()

    async def update_password(self, user_id: int, password: str) -> None:
        validate_password(password)
        now = now_iso()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE auth_users SET password_hash=?,password_changed_at=?,legacy_claim_required=0 WHERE id=?", (hash_password(password), now, int(user_id)))
            await db.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, int(user_id)))
            await db.execute("DELETE FROM trusted_devices WHERE user_id=?", (int(user_id),))
            await db.commit()

    async def update_user(self, user_id: int, *, status: str | None = None, role: str | None = None, account_id: str | None = None, binding_sync_status: str | None = None, ban_reason: str | None = None) -> dict[str, Any] | None:
        if status and status not in {"pending", "approved", "disabled", "denied"}:
            raise ValueError("invalid user status")
        if role and role not in {"user", "admin", "superadmin"}:
            raise ValueError("invalid user role")
        fields: list[str] = []
        values: list[Any] = []
        if status is not None:
            fields.extend(["status=?", "approved_at=?"])
            values.extend([status, now_iso() if status == "approved" else None])
        if role is not None:
            fields.append("role=?")
            values.append(role)
        if account_id is not None:
            fields.append("account_id=?")
            values.append(account_id)
        if binding_sync_status is not None:
            fields.append("binding_sync_status=?")
            values.append(binding_sync_status)
        if ban_reason is not None:
            fields.append("ban_reason=?")
            values.append(ban_reason[:1000] or None)
        if not fields:
            return await self.get_user(user_id)
        values.append(int(user_id))
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"UPDATE auth_users SET {','.join(fields)} WHERE id=?", tuple(values))
            if status in {"disabled", "denied"}:
                await db.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now_iso(), int(user_id)))
                await db.execute("DELETE FROM trusted_devices WHERE user_id=?", (int(user_id),))
            await db.commit()
        return await self.get_user(user_id)

    async def consume_challenge(self, token: str, kind: str, *, new_password: str | None = None) -> dict[str, Any]:
        challenge = await self.get_challenge(token, kind=kind)
        if not challenge or challenge.get("status") != "claimed" or not challenge.get("user_id"):
            raise ValueError("challenge has not been confirmed by Telegram")
        user_id = int(challenge["user_id"])
        if kind == "password_reset":
            if new_password is None:
                raise ValueError("new password is required")
            await self.update_password(user_id, new_password)
        now = now_iso()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE auth_challenges SET status='consumed',used_at=? WHERE id=? AND status='claimed'", (now, challenge["id"]))
            await db.execute("INSERT INTO auth_audit_events(user_id,action,metadata,created_at) VALUES(?,?,?,?)", (user_id, f"{kind}_completed", "{}", now))
            await db.commit()
        return (await self.get_user(user_id)) or {}

    async def audit(self, user_id: int | None, action: str, metadata: str = "{}") -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO auth_audit_events(user_id,action,metadata,created_at) VALUES(?,?,?,?)", (user_id, action, metadata[:4000], now_iso()))
            await db.commit()

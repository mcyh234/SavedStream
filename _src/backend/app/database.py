from __future__ import annotations

import hashlib
import binascii
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite


DEFAULT_CACHE_BYTES = 20 * 1024 * 1024 * 1024
TRAFFIC_GB_BYTES = 1000**3
TRAFFIC_TB_BYTES = 1000**4
DEFAULT_TRAFFIC_CAPACITY_BYTES = TRAFFIC_TB_BYTES
DEFAULT_TRAFFIC_LIMIT_BYTES = 900 * TRAFFIC_GB_BYTES
DEFAULT_HELPER_PER_USER_FILES_24H = 20
DEFAULT_HELPER_PER_USER_BYTES_24H = 10 * TRAFFIC_GB_BYTES
DEFAULT_HELPER_PER_USER_CONCURRENT = 2
DEFAULT_HELPER_MAX_FILE_BYTES = 2 * TRAFFIC_GB_BYTES
DEFAULT_HELPER_GLOBAL_FILES_PER_MINUTE = 30
DEFAULT_HELPER_MAX_ALBUM_ITEMS = 10
DEFAULT_HELPER_MAX_ALBUM_BYTES = 2 * TRAFFIC_GB_BYTES
SYSTEM_BACKUP_MARKER = "#savedstream-system-backup:v1"

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username_normalized TEXT UNIQUE,
    username_display TEXT,
    password_hash TEXT,
    role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin','superadmin')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','disabled','denied')),
    telegram_user_id TEXT UNIQUE,
    telegram_username TEXT,
    display_name TEXT NOT NULL DEFAULT '',
    account_id TEXT,
    binding_sync_status TEXT NOT NULL DEFAULT 'pending' CHECK(binding_sync_status IN ('pending','ready','error','not_required')),
    legacy_claim_required INTEGER NOT NULL DEFAULT 0,
    ban_reason TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    last_login_at TEXT,
    password_changed_at TEXT
);
CREATE INDEX IF NOT EXISTS auth_users_status_idx ON auth_users(status,created_at);
CREATE INDEX IF NOT EXISTS auth_users_telegram_idx ON auth_users(telegram_user_id);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    browser_id_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS auth_sessions_user_idx ON auth_sessions(user_id,expires_at);

CREATE TABLE IF NOT EXISTS auth_challenges (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('register','device_verify','password_reset','legacy_claim')),
    user_id INTEGER,
    username_normalized TEXT,
    username_display TEXT,
    password_hash TEXT,
    browser_id_hash TEXT,
    trust_requested INTEGER NOT NULL DEFAULT 0,
    telegram_user_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','claimed','consumed','expired')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    used_at TEXT,
    FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS auth_challenges_lookup_idx ON auth_challenges(token_hash,kind,status,expires_at);

CREATE TABLE IF NOT EXISTS trusted_devices (
    user_id INTEGER NOT NULL,
    browser_id_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    PRIMARY KEY(user_id,browser_id_hash),
    FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS auth_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS auth_audit_events_user_idx ON auth_audit_events(user_id,created_at);

CREATE TABLE IF NOT EXISTS auth_rate_limits (
    bucket TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    window_started_at TEXT NOT NULL,
    locked_until TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date_parts(value: str | None) -> tuple[str, int, str, str]:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    canonical = parsed.isoformat()
    return canonical, parsed.year, parsed.strftime("%Y-%m"), parsed.strftime("%Y-%m-%d")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fts_available = False

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

                CREATE TABLE IF NOT EXISTS media_index (
                    account_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('video', 'image', 'audio', 'file')),
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    original_title TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    message_date TEXT NOT NULL,
                    date_year INTEGER NOT NULL,
                    date_month TEXT NOT NULL,
                    date_day TEXT NOT NULL,
                    duration REAL,
                    width INTEGER,
                    height INTEGER,
                    has_thumbnail INTEGER NOT NULL DEFAULT 0,
                    visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('public', 'private')),
                    hidden INTEGER NOT NULL DEFAULT 0,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    indexed_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    source_ingest_job_id INTEGER,
                    submitter_telegram_user_id TEXT,
                    requested_visibility TEXT NOT NULL DEFAULT 'private' CHECK(requested_visibility IN ('private', 'public')),
                    review_status TEXT NOT NULL DEFAULT 'not_required' CHECK(review_status IN ('not_required', 'pending', 'approved', 'rejected', 'revoked')),
                    review_reason TEXT,
                    reviewed_at TEXT,
                    reviewed_by TEXT,
                    review_batch_id TEXT,
                    PRIMARY KEY(account_id, message_id)
                );

                CREATE INDEX IF NOT EXISTS media_index_list_idx
                ON media_index(account_id, visibility, deleted, message_id);
                CREATE INDEX IF NOT EXISTS media_index_time_idx
                ON media_index(account_id, visibility, deleted, date_year, date_month, date_day, message_id);
                CREATE INDEX IF NOT EXISTS media_index_kind_idx
                ON media_index(account_id, visibility, deleted, kind, message_id);
                CREATE TABLE IF NOT EXISTS media_sync_state (
                    account_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'idle',
                    mode TEXT NOT NULL DEFAULT 'incremental',
                    cursor INTEGER,
                    high_watermark_id INTEGER,
                    indexed_count INTEGER NOT NULL DEFAULT 0,
                    last_sync_at TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ingest_reconcile_state (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    last_updated_at INTEGER NOT NULL DEFAULT 0,
                    last_job_id INTEGER NOT NULL DEFAULT 0,
                    last_run_at TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS media_review_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    review_batch_id TEXT,
                    decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected', 'revoked')),
                    reason TEXT,
                    reviewed_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS media_review_events_lookup_idx
                ON media_review_events(account_id, message_id, created_at);

                CREATE TABLE IF NOT EXISTS media_deletion_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    source_ingest_job_id INTEGER,
                    submitter_telegram_user_id TEXT,
                    reason TEXT,
                    deleted_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS media_deletion_events_lookup_idx
                ON media_deletion_events(account_id, message_id, created_at);

                CREATE TABLE IF NOT EXISTS review_sync_outbox (
                    job_id INTEGER PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected', 'revoked')),
                    reason TEXT,
                    reviewed_by TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    next_attempt_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS media_timeline_buckets (
                    account_id TEXT NOT NULL,
                    visibility TEXT NOT NULL CHECK(visibility IN ('public', 'private')),
                    date_year INTEGER NOT NULL,
                    date_month TEXT NOT NULL,
                    date_day TEXT NOT NULL,
                    item_count INTEGER NOT NULL,
                    first_message_id INTEGER NOT NULL,
                    last_message_id INTEGER NOT NULL,
                    PRIMARY KEY(account_id, visibility, date_day)
                );

                CREATE INDEX IF NOT EXISTS media_timeline_month_idx
                ON media_timeline_buckets(account_id, visibility, date_year, date_month, date_day);

                CREATE TABLE IF NOT EXISTS upload_jobs (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    bytes_sent INTEGER NOT NULL DEFAULT 0,
                    message_id INTEGER,
                    error TEXT,
                    temp_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS media_folders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_id INTEGER NOT NULL DEFAULT 0,
                    name TEXT NOT NULL,
                    created_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS media_folders_sibling_idx
                ON media_folders(parent_id, name);

                CREATE TABLE IF NOT EXISTS media_folder_items (
                    folder_id INTEGER NOT NULL,
                    account_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(folder_id, account_id, message_id),
                    FOREIGN KEY(folder_id) REFERENCES media_folders(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS media_folder_items_media_idx
                ON media_folder_items(account_id, message_id);

                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    link TEXT,
                    is_read INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    read_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS notifications_user_idx
                ON notifications(user_id, is_read, id);

                CREATE INDEX IF NOT EXISTS notifications_created_idx
                ON notifications(user_id, created_at);

                CREATE TABLE IF NOT EXISTS media_likes (
                    user_id INTEGER NOT NULL,
                    account_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, account_id, message_id),
                    FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS media_likes_media_idx
                ON media_likes(account_id, message_id, created_at);

                CREATE TABLE IF NOT EXISTS media_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reporter_user_id INTEGER NOT NULL,
                    account_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    owner_user_id INTEGER,
                    reason_code TEXT NOT NULL,
                    details TEXT,
                    media_title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','processing','resolved','ignored','failed')),
                    resolution_action TEXT,
                    resolution_reason TEXT,
                    resolved_by INTEGER,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    FOREIGN KEY(reporter_user_id) REFERENCES auth_users(id) ON DELETE CASCADE,
                    FOREIGN KEY(owner_user_id) REFERENCES auth_users(id) ON DELETE SET NULL,
                    FOREIGN KEY(resolved_by) REFERENCES auth_users(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS media_reports_status_idx
                ON media_reports(status, account_id, message_id, id);

                CREATE UNIQUE INDEX IF NOT EXISTS media_reports_open_reporter_idx
                ON media_reports(reporter_user_id, account_id, message_id) WHERE status='open';

                CREATE TABLE IF NOT EXISTS user_sanctions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    sanction_type TEXT NOT NULL CHECK(sanction_type IN ('upload_mute','login_ban','report_mute')),
                    reason TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    expires_at TEXT,
                    created_by INTEGER,
                    revoked_at TEXT,
                    revoked_by INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE CASCADE,
                    FOREIGN KEY(created_by) REFERENCES auth_users(id) ON DELETE SET NULL,
                    FOREIGN KEY(revoked_by) REFERENCES auth_users(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS user_sanctions_active_idx
                ON user_sanctions(user_id, sanction_type, revoked_at, expires_at);

                CREATE TABLE IF NOT EXISTS content_deletion_jobs (
                    id TEXT PRIMARY KEY,
                    target_user_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','partial','failed','cancelled')),
                    total_items INTEGER NOT NULL DEFAULT 0,
                    processed_items INTEGER NOT NULL DEFAULT 0,
                    failed_items INTEGER NOT NULL DEFAULT 0,
                    created_by INTEGER,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY(target_user_id) REFERENCES auth_users(id) ON DELETE CASCADE,
                    FOREIGN KEY(created_by) REFERENCES auth_users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS content_deletion_job_items (
                    job_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','completed','failed')),
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, account_id, message_id),
                    FOREIGN KEY(job_id) REFERENCES content_deletion_jobs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS content_deletion_jobs_user_idx
                ON content_deletion_jobs(target_user_id, created_at);

                CREATE TABLE IF NOT EXISTS traffic_usage_buckets (
                    bucket_type TEXT NOT NULL CHECK(bucket_type IN ('day', 'month')),
                    bucket_start TEXT NOT NULL,
                    bytes_in INTEGER NOT NULL DEFAULT 0,
                    bytes_out INTEGER NOT NULL DEFAULT 0,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(bucket_type, bucket_start)
                );

                CREATE INDEX IF NOT EXISTS traffic_usage_bucket_lookup_idx
                ON traffic_usage_buckets(bucket_type, bucket_start);

                CREATE TABLE IF NOT EXISTS system_backup_settings (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    cron_expr TEXT NOT NULL DEFAULT '0 3 * * *',
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    account_id TEXT,
                    passphrase_salt TEXT,
                    passphrase_nonce TEXT,
                    passphrase_ciphertext TEXT,
                    next_run_at TEXT,
                    last_run_at TEXT,
                    last_status TEXT NOT NULL DEFAULT 'idle',
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS system_backups (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('scheduled','manual','upload','telegram')),
                    status TEXT NOT NULL DEFAULT 'available',
                    created_at TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT NOT NULL DEFAULT '',
                    account_id TEXT,
                    message_id INTEGER,
                    manifest_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    imported_at TEXT,
                    UNIQUE(account_id, message_id)
                );

                CREATE INDEX IF NOT EXISTS system_backups_created_idx
                ON system_backups(created_at DESC);

                CREATE TABLE IF NOT EXISTS system_backup_jobs (
                    id TEXT PRIMARY KEY,
                    backup_id TEXT,
                    trigger TEXT NOT NULL CHECK(trigger IN ('scheduled','manual','upload','telegram')),
                    status TEXT NOT NULL CHECK(status IN ('queued','running','uploading','downloading','validating','restoring','completed','failed','rolled_back')),
                    phase TEXT NOT NULL DEFAULT 'queued',
                    progress REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    temp_path TEXT,
                    error TEXT,
                    created_by INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY(backup_id) REFERENCES system_backups(id) ON DELETE SET NULL,
                    FOREIGN KEY(created_by) REFERENCES auth_users(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS system_backup_jobs_status_idx
                ON system_backup_jobs(status, updated_at);

                CREATE TABLE IF NOT EXISTS traffic_limit_settings (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    monthly_capacity_bytes INTEGER NOT NULL,
                    monthly_limit_bytes INTEGER NOT NULL,
                    warning_percent INTEGER NOT NULL DEFAULT 80,
                    admin_bypass INTEGER NOT NULL DEFAULT 0,
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    updated_at TEXT NOT NULL
                );

                INSERT OR IGNORE INTO media_metadata_v2(account_id, message_id, local_title, updated_at)
                SELECT 'default', message_id, local_title, updated_at FROM media_metadata;
                """
            )
            await db.executescript(AUTH_SCHEMA)
            # Existing installations predate Helper Bot provenance fields.
            # Add them in place without rebuilding or deleting the media index.
            columns_cursor = await db.execute("PRAGMA table_info(media_index)")
            media_columns = {str(row[1]) for row in await columns_cursor.fetchall()}
            if "source_ingest_job_id" not in media_columns:
                await db.execute("ALTER TABLE media_index ADD COLUMN source_ingest_job_id INTEGER")
            if "submitter_telegram_user_id" not in media_columns:
                await db.execute("ALTER TABLE media_index ADD COLUMN submitter_telegram_user_id TEXT")
            if "requested_visibility" not in media_columns:
                await db.execute("ALTER TABLE media_index ADD COLUMN requested_visibility TEXT NOT NULL DEFAULT 'private'")
            if "review_status" not in media_columns:
                await db.execute("ALTER TABLE media_index ADD COLUMN review_status TEXT NOT NULL DEFAULT 'not_required'")
            if "review_reason" not in media_columns:
                await db.execute("ALTER TABLE media_index ADD COLUMN review_reason TEXT")
            if "reviewed_at" not in media_columns:
                await db.execute("ALTER TABLE media_index ADD COLUMN reviewed_at TEXT")
            if "reviewed_by" not in media_columns:
                await db.execute("ALTER TABLE media_index ADD COLUMN reviewed_by TEXT")
            if "review_batch_id" not in media_columns:
                await db.execute("ALTER TABLE media_index ADD COLUMN review_batch_id TEXT")
            if "hidden" not in media_columns:
                await db.execute("ALTER TABLE media_index ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
            if "owner_user_id" not in media_columns:
                await db.execute("ALTER TABLE media_index ADD COLUMN owner_user_id INTEGER")
            if "upload_source" not in media_columns:
                await db.execute("ALTER TABLE media_index ADD COLUMN upload_source TEXT NOT NULL DEFAULT 'legacy'")
            if "upload_batch_id" not in media_columns:
                await db.execute("ALTER TABLE media_index ADD COLUMN upload_batch_id TEXT")
            upload_columns_cursor = await db.execute("PRAGMA table_info(upload_jobs)")
            upload_columns = {str(row[1]) for row in await upload_columns_cursor.fetchall()}
            for column_name, definition in (
                ("owner_user_id", "INTEGER"),
                ("submitter_telegram_user_id", "TEXT"),
                ("requested_visibility", "TEXT NOT NULL DEFAULT 'private'"),
                ("review_status", "TEXT NOT NULL DEFAULT 'not_required'"),
                ("batch_id", "TEXT"),
                ("upload_source", "TEXT NOT NULL DEFAULT 'web'"),
                ("quota_reservation_key", "TEXT"),
            ):
                if column_name not in upload_columns:
                    await db.execute(f"ALTER TABLE upload_jobs ADD COLUMN {column_name} {definition}")
            # These indexes reference columns added by migrations above.  They
            # must be created after the ALTER TABLE statements so an upgrade
            # from the pre-review schema does not fail during startup.
            await db.execute(
                "CREATE INDEX IF NOT EXISTS media_index_review_idx "
                "ON media_index(review_status, deleted, message_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS media_index_owner_idx "
                "ON media_index(submitter_telegram_user_id, account_id, deleted, message_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS media_index_owner_user_idx "
                "ON media_index(owner_user_id, requested_visibility, deleted, message_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS media_index_upload_batch_idx "
                "ON media_index(upload_batch_id, owner_user_id, deleted)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS upload_jobs_owner_idx "
                "ON upload_jobs(owner_user_id, created_at)"
            )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS media_index_ingest_job_idx "
                "ON media_index(source_ingest_job_id) WHERE source_ingest_job_id IS NOT NULL"
            )
            await db.execute(
                "INSERT OR IGNORE INTO ingest_reconcile_state(id,last_updated_at,last_job_id,updated_at) "
                "VALUES(1,0,0,?)",
                (_now(),),
            )
            # Backfill review metadata without deleting any media.  Legacy
            # Helper Bot publications are deliberately demoted to the review
            # queue so an upgrade cannot silently expose old uploads.
            await db.execute(
                "UPDATE media_index SET requested_visibility=CASE WHEN visibility='public' THEN 'public' ELSE 'private' END, "
                "review_status=CASE WHEN visibility='public' THEN 'approved' ELSE 'not_required' END "
                "WHERE source_ingest_job_id IS NULL AND (review_status IS NULL OR review_status='not_required')"
            )
            await db.execute(
                "UPDATE media_index SET requested_visibility='public', review_status='pending', visibility='private' "
                "WHERE source_ingest_job_id IS NOT NULL AND visibility='public' "
                "AND (review_status IS NULL OR review_status IN ('not_required','approved'))"
            )
            try:
                await db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS media_index_fts USING fts5("
                    "account_id UNINDEXED, message_id UNINDEXED, title, filename, caption)"
                )
                self._fts_available = True
            except Exception:
                # SQLite builds without FTS5 continue with the indexed LIKE
                # fallback; the media index remains fully functional.
                self._fts_available = False
            if self._fts_available:
                # Backfill rows created before FTS5 was introduced.  This is
                # idempotent and keeps an update from making existing indexed
                # media disappear from local search.
                await db.execute(
                    "INSERT INTO media_index_fts(account_id,message_id,title,filename,caption) "
                    "SELECT m.account_id,m.message_id,"
                    "       COALESCE(t.local_title,'') || ' ' || m.original_title,"
                    "       m.filename,m.caption "
                    "FROM media_index m "
                    "LEFT JOIN media_metadata_v2 t ON t.account_id=m.account_id AND t.message_id=m.message_id "
                    "WHERE NOT EXISTS ("
                    "  SELECT 1 FROM media_index_fts f "
                    "  WHERE f.account_id=m.account_id AND f.message_id=m.message_id"
                    ")"
                )
            await db.executemany(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                [
                    ("cache_max_bytes", str(DEFAULT_CACHE_BYTES)),
                    ("access_restricted", "0"),
                    ("viewer_key_hash", ""),
                    ("public_album_enabled", "0"),
                    ("public_album_key_hash", ""),
                    ("public_album_key_version", "1"),
                    ("public_registration_enabled", "0"),
                    ("registration_requires_approval", "1"),
                    ("registration_key_hash", ""),
                    ("registration_key_version", "1"),
                    ("registration_key_fingerprint", ""),
                ],
            )
            # Existing installations used the public album key before account
            # authentication existed.  Reuse its hash only as the initial
            # registration key; normal media access no longer accepts it.
            await db.execute(
                "UPDATE settings SET value=(SELECT value FROM settings WHERE key='public_album_key_hash') "
                "WHERE key='registration_key_hash' AND value='' "
                "AND EXISTS(SELECT 1 FROM settings WHERE key='public_album_key_hash' AND value!='')"
            )
            await db.execute(
                "UPDATE settings SET value=(SELECT value FROM settings WHERE key='public_album_key_version') "
                "WHERE key='registration_key_version' AND value='1' "
                "AND EXISTS(SELECT 1 FROM settings WHERE key='public_album_key_version')"
            )
            # Preserve old Telegram users and approvals while requiring a
            # one-time username/password claim in the new authentication UI.
            await db.execute(
                "INSERT OR IGNORE INTO auth_users("
                "username_normalized,username_display,password_hash,role,status,telegram_user_id,telegram_username,"
                "display_name,account_id,binding_sync_status,legacy_claim_required,created_at,approved_at,last_login_at) "
                "SELECT NULL,NULL,NULL,'user',m.status,m.telegram_user_id,m.username,m.display_name,m.account_id,"
                "CASE WHEN m.status='approved' THEN 'ready' ELSE 'pending' END,1,m.requested_at,m.approved_at,m.last_login_at "
                "FROM media_users m WHERE NOT EXISTS(SELECT 1 FROM auth_users a WHERE a.telegram_user_id=m.telegram_user_id)"
            )
            await db.execute(
                "UPDATE media_index SET owner_user_id=(SELECT a.id FROM auth_users a "
                "WHERE a.telegram_user_id=media_index.submitter_telegram_user_id) "
                "WHERE owner_user_id IS NULL AND submitter_telegram_user_id IS NOT NULL"
            )
            # Old one-time web sessions are intentionally invalidated on the
            # first upgraded startup.  The legacy claim flow replaces them.
            await db.execute("DELETE FROM access_sessions")
            await db.execute(
                "INSERT OR IGNORE INTO traffic_limit_settings("
                "id,enabled,monthly_capacity_bytes,monthly_limit_bytes,warning_percent,admin_bypass,timezone,updated_at) "
                "VALUES(1,0,?,?,?,?,?,?)",
                (
                    DEFAULT_TRAFFIC_CAPACITY_BYTES,
                    DEFAULT_TRAFFIC_LIMIT_BYTES,
                    80,
                    0,
                    "UTC",
                    _now(),
                ),
            )
            await db.execute(
                "INSERT OR IGNORE INTO system_backup_settings(id,updated_at) VALUES(1,?)",
                (_now(),),
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

    async def get_system_backup_settings(self) -> dict[str, Any]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM system_backup_settings WHERE id=1")
            row = await cursor.fetchone()
        return dict(row) if row else {
            "id": 1,
            "enabled": 0,
            "cron_expr": "0 3 * * *",
            "timezone": "UTC",
            "account_id": None,
            "passphrase_salt": None,
            "passphrase_nonce": None,
            "passphrase_ciphertext": None,
            "next_run_at": None,
            "last_run_at": None,
            "last_status": "idle",
            "last_error": None,
            "updated_at": _now(),
        }

    async def update_system_backup_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "enabled", "cron_expr", "timezone", "account_id", "passphrase_salt",
            "passphrase_nonce", "passphrase_ciphertext", "next_run_at", "last_run_at",
            "last_status", "last_error",
        }
        values = {key: value for key, value in values.items() if key in allowed}
        values["updated_at"] = _now()
        assignments = ",".join(f"{key}=?" for key in values)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"UPDATE system_backup_settings SET {assignments} WHERE id=1",
                tuple(values.values()),
            )
            await db.commit()
        return await self.get_system_backup_settings()

    async def create_system_backup(self, item: dict[str, Any]) -> dict[str, Any]:
        fields = [
            "id", "filename", "source", "status", "created_at", "size_bytes", "sha256",
            "account_id", "message_id", "manifest_json", "error", "imported_at",
        ]
        values = [item.get(field) for field in fields]
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"INSERT INTO system_backups({','.join(fields)}) VALUES({','.join('?' for _ in fields)}) "
                "ON CONFLICT(id) DO UPDATE SET filename=excluded.filename,status=excluded.status,"
                "size_bytes=excluded.size_bytes,sha256=excluded.sha256,account_id=excluded.account_id,"
                "message_id=excluded.message_id,manifest_json=excluded.manifest_json,error=excluded.error,"
                "imported_at=excluded.imported_at",
                values,
            )
            await db.commit()
        return await self.get_system_backup(str(item["id"])) or dict(item)

    async def get_system_backup(self, backup_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM system_backups WHERE id=?", (backup_id,))
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_system_backups(self, limit: int = 200) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM system_backups ORDER BY created_at DESC LIMIT ?", (max(1, min(1000, limit)),))
            return [dict(row) for row in await cursor.fetchall()]

    async def update_system_backup(self, backup_id: str, **values: Any) -> dict[str, Any] | None:
        allowed = {"filename", "status", "size_bytes", "sha256", "account_id", "message_id", "manifest_json", "error", "imported_at"}
        values = {key: value for key, value in values.items() if key in allowed}
        if not values:
            return await self.get_system_backup(backup_id)
        assignments = ",".join(f"{key}=?" for key in values)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"UPDATE system_backups SET {assignments} WHERE id=?", (*values.values(), backup_id))
            await db.commit()
        return await self.get_system_backup(backup_id)

    async def create_system_backup_job(self, item: dict[str, Any]) -> dict[str, Any]:
        fields = ["id", "backup_id", "trigger", "status", "phase", "progress", "attempts", "temp_path", "error", "created_by", "created_at", "updated_at", "completed_at"]
        values = [item.get(field) for field in fields]
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"INSERT INTO system_backup_jobs({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", values)
            await db.commit()
        return await self.get_system_backup_job(str(item["id"])) or dict(item)

    async def get_system_backup_job(self, job_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM system_backup_jobs WHERE id=?", (job_id,))
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_system_backup_job(self, job_id: str, **values: Any) -> dict[str, Any] | None:
        values = {key: value for key, value in values.items() if key in {"backup_id", "status", "phase", "progress", "attempts", "temp_path", "error", "updated_at", "completed_at"}}
        values.setdefault("updated_at", _now())
        if not values:
            return await self.get_system_backup_job(job_id)
        assignments = ",".join(f"{key}=?" for key in values)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"UPDATE system_backup_jobs SET {assignments} WHERE id=?", (*values.values(), job_id))
            await db.commit()
        return await self.get_system_backup_job(job_id)

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
        now = _now()
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
        now = _now()
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

    async def ban_media_user(
        self,
        telegram_user_id: str,
        account_id: str,
        *,
        username: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, str | None]:
        """Create or persist a disabled media user for a Helper Bot submitter.

        Helper Bot uploaders do not necessarily have a SavedStream web-login
        row yet.  A review ban must still be durable in SavedStream so a later
        web login cannot silently recreate an approved user.
        """
        user_id = str(telegram_user_id)
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO media_users(telegram_user_id,account_id,username,display_name,status,requested_at,approved_at,last_login_at) "
                "VALUES(?,?,?,?, 'disabled', ?, NULL, ?) "
                "ON CONFLICT(telegram_user_id) DO UPDATE SET account_id=excluded.account_id, "
                "username=COALESCE(excluded.username, media_users.username), "
                "display_name=COALESCE(NULLIF(excluded.display_name,''), media_users.display_name), "
                "status='disabled'",
                (
                    user_id,
                    str(account_id),
                    username,
                    display_name or f"Telegram {user_id}",
                    now,
                    now,
                ),
            )
            await db.execute("DELETE FROM access_sessions WHERE telegram_user_id=?", (user_id,))
            await db.commit()
        record = await self.get_media_user(user_id)
        assert record is not None
        return record

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
        now = _now()
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
        now = _now()
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
        return {
            "fingerprint": str(row[0]),
            "public_key_pem": str(row[1]),
            "created_at": str(row[2]),
            "last_used_at": str(row[3]),
            "revoked": int(row[4]),
        }

    async def touch_device_key(self, fingerprint: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE device_keys SET last_used_at=? WHERE fingerprint=?", (_now(), fingerprint))
            await db.commit()

    async def revoke_device_key(self, fingerprint: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE device_keys SET last_used_at=?, revoked=1 WHERE fingerprint=?", (_now(), fingerprint))
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
                    (account_id, message_id, clean_title, _now()),
                )
            else:
                await db.execute(
                    "DELETE FROM media_metadata_v2 WHERE account_id = ? AND message_id = ?", (account_id, message_id)
                )
            await db.commit()
        await self._refresh_search_index(account_id, message_id)

    async def _refresh_search_index(self, account_id: str, message_id: int) -> None:
        if not self._fts_available:
            return
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT m.original_title,m.filename,m.caption,COALESCE(t.local_title,'') AS local_title "
                "FROM media_index m LEFT JOIN media_metadata_v2 t ON t.account_id=m.account_id AND t.message_id=m.message_id "
                "WHERE m.account_id=? AND m.message_id=? AND m.deleted=0",
                (account_id, int(message_id)),
            )
            row = await cursor.fetchone()
            await db.execute("DELETE FROM media_index_fts WHERE account_id=? AND message_id=?", (account_id, int(message_id)))
            if row:
                await db.execute(
                    "INSERT INTO media_index_fts(account_id,message_id,title,filename,caption) VALUES(?,?,?,?,?)",
                    (account_id, int(message_id), f"{row['local_title']} {row['original_title']}", row["filename"], row["caption"]),
                )
            await db.commit()

    @staticmethod
    def _media_row(row: aiosqlite.Row | None, *, include_provenance: bool = False) -> dict[str, Any]:
        if not row:
            return {}
        raw = dict(row)
        raw["id"] = int(raw.pop("message_id"))
        # The SQLite column is named message_date, while the public API and
        # frontend MediaItem contract use date.  Keep this translation in one
        # place so indexed rows cannot crash the gallery with date.slice(...).
        raw["date"] = str(raw.pop("message_date", raw.get("date") or _now()))
        raw["size"] = int(raw["size"])
        raw["has_thumbnail"] = bool(raw["has_thumbnail"])
        raw["deleted"] = bool(raw["deleted"])
        raw["hidden"] = bool(raw.get("hidden"))
        raw["like_count"] = int(raw.get("like_count") or 0)
        raw["liked_by_me"] = bool(raw.get("liked_by_me"))
        raw["owned_by_me"] = bool(raw.get("owned_by_me"))
        if raw["hidden"]:
            raw["visibility"] = "hidden"
        raw["duration"] = float(raw["duration"]) if raw["duration"] is not None else None
        raw["width"] = int(raw["width"]) if raw["width"] is not None else None
        raw["height"] = int(raw["height"]) if raw["height"] is not None else None
        raw["local_title"] = raw.get("local_title") or None
        raw["title"] = raw.get("title") or raw["original_title"]
        if not include_provenance:
            # Provenance is used for server-side reconciliation and must not
            # leak Telegram user IDs or internal moderation metadata through
            # the private/square/liked gallery responses.  ``my_public`` and
            # administrator views explicitly opt in so they can render the
            # user's review state and reason.
            raw.pop("source_ingest_job_id", None)
            raw.pop("submitter_telegram_user_id", None)
            raw.pop("owner_user_id", None)
            raw.pop("requested_visibility", None)
            raw.pop("review_status", None)
            raw.pop("review_reason", None)
            raw.pop("reviewed_at", None)
            raw.pop("reviewed_by", None)
            raw.pop("review_batch_id", None)
            raw.pop("upload_source", None)
            raw.pop("upload_batch_id", None)
        return raw

    async def upsert_media_index(
        self,
        item: dict[str, Any],
        visibility: str | None = None,
        *,
        source_ingest_job_id: int | None = None,
        submitter_telegram_user_id: str | None = None,
        owner_user_id: int | None = None,
        requested_visibility: str | None = None,
        review_status: str | None = None,
        review_batch_id: str | None = None,
        upload_source: str | None = None,
        upload_batch_id: str | None = None,
        hidden: bool = False,
    ) -> dict[str, Any]:
        account_id = str(item["account_id"])
        message_id = int(item["id"])
        canonical_date, year, month, day = _date_parts(item.get("date"))
        now = _now()
        effective_visibility = visibility or str(item.get("visibility") or "private")
        if effective_visibility not in {"public", "private"}:
            effective_visibility = "private"
        requested = requested_visibility or str(
            item.get("requested_visibility")
            or ("public" if effective_visibility == "public" else "private")
        )
        if requested not in {"public", "private"}:
            requested = "private"
        status = review_status or str(
            item.get("review_status")
            or ("approved" if effective_visibility == "public" else "pending" if requested == "public" and source_ingest_job_id else "not_required")
        )
        if status not in {"not_required", "pending", "approved", "rejected", "revoked"}:
            status = "not_required"
        is_system_backup = SYSTEM_BACKUP_MARKER in str(item.get("caption") or "") or str(item.get("filename") or "").endswith(".ssbak")
        if is_system_backup:
            effective_visibility = "private"
            requested = "private"
            status = "not_required"
            hidden = True
            upload_source = "system_backup"
        # Helper Bot public requests are never exposed before an explicit
        # administrator approval.  The legacy compatibility path above is
        # retained only for callers that omit review metadata entirely.
        if source_ingest_job_id is not None and status != "approved":
            effective_visibility = "private"
        async with aiosqlite.connect(self.path) as db:
            if owner_user_id is None and submitter_telegram_user_id:
                owner_cursor = await db.execute(
                    "SELECT id FROM auth_users WHERE telegram_user_id=?",
                    (str(submitter_telegram_user_id),),
                )
                owner_row = await owner_cursor.fetchone()
                if owner_row:
                    owner_user_id = int(owner_row[0])
            await db.execute(
                """
                INSERT INTO media_index(
                    account_id,message_id,kind,mime_type,size,filename,original_title,caption,message_date,
                    date_year,date_month,date_day,duration,width,height,has_thumbnail,visibility,hidden,deleted,indexed_at,last_seen_at,
                    source_ingest_job_id,submitter_telegram_user_id,owner_user_id,requested_visibility,review_status,review_batch_id,upload_source,upload_batch_id
                ) VALUES(
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE(?, 'private'), ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(account_id,message_id) DO UPDATE SET
                    kind=excluded.kind,
                    mime_type=excluded.mime_type,
                    size=excluded.size,
                    filename=excluded.filename,
                    original_title=excluded.original_title,
                    caption=excluded.caption,
                    message_date=excluded.message_date,
                    date_year=excluded.date_year,
                    date_month=excluded.date_month,
                    date_day=excluded.date_day,
                    duration=excluded.duration,
                    width=excluded.width,
                    height=excluded.height,
                    has_thumbnail=excluded.has_thumbnail,
                    visibility=CASE
                        WHEN media_index.source_ingest_job_id IS NULL
                             AND excluded.source_ingest_job_id IS NOT NULL
                        THEN excluded.visibility
                        ELSE media_index.visibility
                    END,
                    hidden=CASE
                        WHEN media_index.source_ingest_job_id IS NULL
                             AND excluded.source_ingest_job_id IS NOT NULL
                        THEN excluded.hidden
                        ELSE media_index.hidden
                    END,
                    source_ingest_job_id=COALESCE(media_index.source_ingest_job_id, excluded.source_ingest_job_id),
                    submitter_telegram_user_id=COALESCE(media_index.submitter_telegram_user_id, excluded.submitter_telegram_user_id),
                    owner_user_id=COALESCE(media_index.owner_user_id, excluded.owner_user_id),
                    requested_visibility=CASE
                        WHEN media_index.source_ingest_job_id IS NULL
                             AND excluded.source_ingest_job_id IS NOT NULL
                        THEN excluded.requested_visibility
                        ELSE media_index.requested_visibility
                    END,
                    review_status=CASE
                        WHEN media_index.source_ingest_job_id IS NULL
                             AND excluded.source_ingest_job_id IS NOT NULL
                        THEN excluded.review_status
                        ELSE media_index.review_status
                    END,
                    review_batch_id=COALESCE(media_index.review_batch_id, excluded.review_batch_id),
                    upload_source=CASE
                        WHEN media_index.upload_source='legacy' THEN excluded.upload_source
                        ELSE media_index.upload_source
                    END,
                    upload_batch_id=COALESCE(media_index.upload_batch_id, excluded.upload_batch_id),
                    -- A policy-deleted/tombstoned row must not be resurrected by
                    -- a later Telegram index pass.  Telegram message IDs are
                    -- stable within Saved Messages, so a row marked deleted is
                    -- terminal until an explicit operator migration restores it.
                    deleted=media_index.deleted,
                    last_seen_at=excluded.last_seen_at
                WHERE media_index.deleted=0
                """,
                (
                    account_id,
                    message_id,
                    str(item.get("kind") or "file"),
                    str(item.get("mime_type") or "application/octet-stream"),
                    int(item.get("size") or 0),
                    str(item.get("filename") or f"saved-{message_id}"),
                    str(item.get("original_title") or item.get("filename") or f"saved-{message_id}"),
                    str(item.get("caption") or ""),
                    canonical_date,
                    year,
                    month,
                    day,
                    item.get("duration"),
                    item.get("width"),
                    item.get("height"),
                    1 if item.get("has_thumbnail") else 0,
                    effective_visibility,
                    1 if hidden else 0,
                    now,
                    now,
                    int(source_ingest_job_id) if source_ingest_job_id is not None else None,
                    str(submitter_telegram_user_id) if submitter_telegram_user_id else None,
                    int(owner_user_id) if owner_user_id is not None else None,
                    requested,
                    status,
                    review_batch_id,
                    str(upload_source or item.get("upload_source") or ("helper_bot" if source_ingest_job_id is not None else "legacy"))[:40],
                    str(upload_batch_id or item.get("upload_batch_id") or "")[:120] or None,
                ),
            )
            await db.commit()
        await self._refresh_search_index(account_id, message_id)
        # Include tombstones here so an index pass can remain idempotent after
        # a policy deletion without treating the deliberately hidden row as a
        # failed upsert.  Public/list callers still exclude deleted rows by
        # default.
        result = await self.get_media_index(
            account_id,
            message_id,
            include_deleted=True,
            include_provenance=bool(source_ingest_job_id or owner_user_id),
        )
        assert result is not None
        return result

    async def upsert_ingest_review_placeholder(
        self,
        job: dict[str, Any],
        *,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        """Keep a pending Helper Bot review visible when Telegram metadata is
        temporarily unavailable.

        The final Saved Message is normally read immediately after the
        userbot import.  A transient TeleBox/Telegram failure must not make a
        user's public request disappear from the administrator queue.  The
        placeholder contains only the durable job metadata; a later normal
        reconciliation pass replaces it with the complete Telegram metadata.
        """
        account_id = str(job.get("account_id") or "").strip()
        message_id = int(job.get("saved_message_id") or 0)
        job_id = int(job.get("id") or 0)
        requested = str(job.get("requested_visibility") or "").strip()
        if not account_id or not message_id or not job_id or requested != "public":
            return None
        review_status = str(job.get("review_status") or "pending").strip()
        if review_status not in {"pending", "approved", "rejected", "revoked"}:
            review_status = "pending"
        mime_type = str(job.get("source_mime_type") or "application/octet-stream")[:200]
        filename = str(job.get("source_filename") or f"saved-{message_id}")[:500]
        if mime_type.startswith("video/"):
            kind = "video"
        elif mime_type.startswith("image/"):
            kind = "image"
        elif mime_type.startswith("audio/"):
            kind = "audio"
        else:
            kind = "file"
        timestamp = int(job.get("created_at") or job.get("updated_at") or 0)
        if timestamp > 100_000_000_000:
            timestamp //= 1000
        try:
            message_date = datetime.fromtimestamp(max(0, timestamp), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            message_date = _now()
        visibility = "public" if review_status == "approved" else "private"
        return await self.upsert_media_index(
            {
                "account_id": account_id,
                "id": message_id,
                "kind": kind,
                "mime_type": mime_type,
                "size": max(0, int(job.get("source_file_size") or 0)),
                "filename": filename,
                "original_title": filename,
                "caption": "",
                "date": message_date,
                "has_thumbnail": False,
            },
            visibility=visibility,
            source_ingest_job_id=job_id,
            submitter_telegram_user_id=str(job.get("submitter_telegram_user_id") or job.get("source_chat_id") or "") or None,
            requested_visibility="public",
            review_status=review_status,
            review_batch_id=str(job.get("review_batch_id") or "") or None,
        )

    async def get_media_index(
        self,
        account_id: str,
        message_id: int,
        *,
        include_deleted: bool = False,
        include_provenance: bool = False,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            deleted_clause = "" if include_deleted else "AND m.deleted=0"
            cursor = await db.execute(
                "SELECT m.*, t.local_title, COALESCE(NULLIF(t.local_title,''), m.original_title) AS title "
                "FROM media_index m LEFT JOIN media_metadata_v2 t ON t.account_id=m.account_id AND t.message_id=m.message_id "
                f"WHERE m.account_id=? AND m.message_id=? {deleted_clause}",
                (account_id, int(message_id)),
            )
            row = await cursor.fetchone()
        return self._media_row(row, include_provenance=include_provenance) if row else None

    @staticmethod
    def _encode_media_cursor(message_id: int, account_id: str) -> str:
        import base64

        raw = f"{int(message_id)}\x00{account_id}".encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_media_cursor(cursor: str | int | None, account_id: str | None) -> tuple[int, str | None] | None:
        if cursor is None or cursor == "":
            return None
        if isinstance(cursor, int) or str(cursor).isdigit():
            return int(cursor), account_id
        import base64

        try:
            padded = str(cursor) + "=" * ((4 - len(str(cursor)) % 4) % 4)
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
            message_id, cursor_account = decoded.split("\x00", 1)
            return int(message_id), cursor_account
        except (ValueError, UnicodeDecodeError, binascii.Error):
            raise ValueError("invalid media cursor")

    async def list_media_index(
        self,
        *,
        account_id: str | None,
        limit: int,
        cursor: str | int | None,
        order: str,
        kind: str,
        query: str,
        visibility: str,
        date_from: str | None = None,
        date_to: str | None = None,
        owner_telegram_user_id: str | None = None,
        owner_user_id: int | None = None,
        owner_account_id: str | None = None,
        collection: str | None = None,
        viewer_user_id: int | None = None,
        include_provenance: bool = False,
        folder_id: int | None = None,
    ) -> tuple[list[dict[str, Any]], str | int | None, bool]:
        clauses = ["m.deleted=0"]
        params: list[Any] = []
        if account_id:
            clauses.append("m.account_id=?")
            params.append(account_id)
        owner_clause = "(m.owner_user_id=? OR (m.owner_user_id IS NULL AND m.submitter_telegram_user_id=?))"
        if collection == "square":
            clauses.extend(["m.visibility='public'", "m.review_status='approved'", "m.hidden=0"])
        elif collection == "private":
            if owner_user_id is None:
                clauses.append("1=0")
            else:
                clauses.extend([owner_clause, "m.requested_visibility='private'", "m.visibility='private'", "m.hidden=0"])
                params.extend([int(owner_user_id), str(owner_telegram_user_id or "")])
        elif collection == "my_public":
            if owner_user_id is None:
                clauses.append("1=0")
            else:
                clauses.extend([owner_clause, "m.requested_visibility='public'", "m.hidden=0"])
                params.extend([int(owner_user_id), str(owner_telegram_user_id or "")])
        elif collection == "liked":
            if viewer_user_id is None:
                clauses.append("1=0")
            else:
                clauses.extend([
                    "m.visibility='public'",
                    "m.review_status='approved'",
                    "m.hidden=0",
                    "EXISTS(SELECT 1 FROM media_likes ml WHERE ml.user_id=? AND ml.account_id=m.account_id AND ml.message_id=m.message_id)",
                ])
                params.append(int(viewer_user_id))
        elif visibility == "public":
            clauses.extend(["m.visibility='public'", "m.review_status='approved'", "m.hidden=0"])
        elif visibility == "private":
            clauses.extend(["m.visibility='private'", "m.hidden=0"])
        elif visibility == "hidden":
            clauses.append("m.hidden=1")
        elif visibility == "all" and owner_telegram_user_id:
            clauses.append(
                "((m.review_status='approved' AND m.visibility='public' AND m.hidden=0) OR "
                "(m.submitter_telegram_user_id=? AND (? IS NULL OR m.account_id=?) AND m.hidden=0))"
            )
            params.extend([owner_telegram_user_id, owner_account_id, owner_account_id])
        elif visibility == "all" and not owner_telegram_user_id:
            # Administrators browsing every album see hidden rows with the
            # mapped hidden visibility; other scopes still exclude them.
            pass
        folder_join = ""
        if folder_id is not None:
            folder_join = "JOIN media_folder_items fi ON fi.account_id=m.account_id AND fi.message_id=m.message_id"
            clauses.append("fi.folder_id=?")
            params.append(int(folder_id))
        if kind != "all":
            clauses.append("m.kind=?")
            params.append(kind)
        clean_query = query.strip().lower()
        fts_join = ""
        if clean_query:
            fts_query = clean_query.replace('"', " ").replace("'", " ").replace(":", " ").strip()
            if self._fts_available and fts_query:
                fts_join = "JOIN media_index_fts f ON f.account_id=m.account_id AND f.message_id=m.message_id"
                clauses.append("f.media_index_fts MATCH ?")
                params.append(fts_query)
            else:
                clauses.append("lower(COALESCE(t.local_title,'') || ' ' || m.original_title || ' ' || m.filename || ' ' || m.caption) LIKE ?")
                params.append(f"%{clean_query}%")
        if date_from:
            clauses.append("m.date_day>=?")
            params.append(date_from)
        if date_to:
            clauses.append("m.date_day<=?")
            params.append(date_to)
        direction = "ASC" if order == "oldest" else "DESC"
        decoded_cursor = self._decode_media_cursor(cursor, account_id)
        if decoded_cursor is not None:
            cursor_id, cursor_account = decoded_cursor
            direction = "ASC" if order == "oldest" else "DESC"
            operator = ">" if order == "oldest" else "<"
            if cursor_account is None:
                clauses.append(f"m.message_id {operator} ?")
                params.append(cursor_id)
            else:
                clauses.append(
                    f"(m.message_id {operator} ? OR (m.message_id=? AND m.account_id {operator} ?))"
                )
                params.extend([cursor_id, cursor_id, cursor_account])
        params.append(int(limit) + 1)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            viewer_id = int(viewer_user_id) if viewer_user_id is not None else -1
            cursor_obj = await db.execute(
                "SELECT m.*, t.local_title, COALESCE(NULLIF(t.local_title,''), m.original_title) AS title, "
                "(SELECT COUNT(*) FROM media_likes lc WHERE lc.account_id=m.account_id AND lc.message_id=m.message_id) AS like_count, "
                "EXISTS(SELECT 1 FROM media_likes lm WHERE lm.user_id=? AND lm.account_id=m.account_id AND lm.message_id=m.message_id) AS liked_by_me, "
                "CASE WHEN m.owner_user_id=? OR (m.owner_user_id IS NULL AND m.submitter_telegram_user_id=?) THEN 1 ELSE 0 END AS owned_by_me "
                "FROM media_index m LEFT JOIN media_metadata_v2 t ON t.account_id=m.account_id AND t.message_id=m.message_id "
                f"{fts_join} {folder_join} "
                f"WHERE {' AND '.join(clauses)} ORDER BY m.message_id {direction}, m.account_id {direction} LIMIT ?",
                [viewer_id, viewer_id, str(owner_telegram_user_id or ""), *params],
            )
            rows = await cursor_obj.fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [self._media_row(row, include_provenance=include_provenance) for row in rows]
        if not has_more or not items:
            return items, None, has_more
        next_cursor: str | int
        if account_id:
            next_cursor = items[-1]["id"]
        else:
            next_cursor = self._encode_media_cursor(items[-1]["id"], str(items[-1]["account_id"]))
        return items, next_cursor, has_more

    async def rebuild_timeline(self, account_id: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM media_timeline_buckets WHERE account_id=?", (account_id,))
            await db.execute(
                """
                INSERT INTO media_timeline_buckets(
                    account_id,visibility,date_year,date_month,date_day,item_count,first_message_id,last_message_id
                )
                SELECT account_id,
                       CASE WHEN visibility='public' AND review_status='approved' THEN 'public' ELSE 'private' END,
                       date_year,date_month,date_day,COUNT(*),MIN(message_id),MAX(message_id)
                FROM media_index
                WHERE account_id=? AND deleted=0 AND hidden=0
                GROUP BY account_id,visibility,date_year,date_month,date_day
                """,
                (account_id,),
            )
            await db.commit()

    async def list_timeline(
        self,
        *,
        account_id: str | None,
        visibility: str,
        kind: str = "all",
        query: str = "",
        owner_telegram_user_id: str | None = None,
        owner_user_id: int | None = None,
        collection: str | None = None,
        viewer_user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        if account_id and collection is None and not owner_telegram_user_id and kind == "all" and not query.strip() and visibility in {"public", "private"}:
            clauses = ["account_id=?"]
            params: list[Any] = [account_id]
            clauses.append("visibility=?")
            params.append(visibility)
            async with aiosqlite.connect(self.path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT date_year,date_month,date_day,item_count,first_message_id,last_message_id "
                    f"FROM media_timeline_buckets WHERE {' AND '.join(clauses)} ORDER BY date_day DESC",
                    params,
                )
                rows = await cursor.fetchall()
            if rows:
                return self._timeline_tree(rows)

        clauses = ["m.deleted=0"]
        params: list[Any] = []
        if account_id:
            clauses.append("m.account_id=?")
            params.append(account_id)
        owner_clause = "(m.owner_user_id=? OR (m.owner_user_id IS NULL AND m.submitter_telegram_user_id=?))"
        if collection == "square":
            clauses.extend(["m.visibility='public'", "m.review_status='approved'", "m.hidden=0"])
        elif collection == "private":
            if owner_user_id is None:
                clauses.append("1=0")
            else:
                clauses.extend([owner_clause, "m.requested_visibility='private'", "m.visibility='private'", "m.hidden=0"])
                params.extend([int(owner_user_id), str(owner_telegram_user_id or "")])
        elif collection == "my_public":
            if owner_user_id is None:
                clauses.append("1=0")
            else:
                clauses.extend([owner_clause, "m.requested_visibility='public'", "m.hidden=0"])
                params.extend([int(owner_user_id), str(owner_telegram_user_id or "")])
        elif collection == "liked":
            if viewer_user_id is None:
                clauses.append("1=0")
            else:
                clauses.extend([
                    "m.visibility='public'",
                    "m.review_status='approved'",
                    "m.hidden=0",
                    "EXISTS(SELECT 1 FROM media_likes ml WHERE ml.user_id=? AND ml.account_id=m.account_id AND ml.message_id=m.message_id)",
                ])
                params.append(int(viewer_user_id))
        elif visibility == "public":
            clauses.extend(["m.visibility='public'", "m.review_status='approved'", "m.hidden=0"])
        elif visibility == "private":
            clauses.extend(["m.visibility='private'", "m.hidden=0"])
        elif visibility == "hidden":
            clauses.append("m.hidden=1")
        elif visibility == "all" and owner_telegram_user_id:
            clauses.append(
                "((m.visibility='public' AND m.review_status='approved' AND m.hidden=0) OR "
                "(m.submitter_telegram_user_id=? AND m.hidden=0))"
            )
            params.append(owner_telegram_user_id)
        if kind != "all":
            clauses.append("m.kind=?")
            params.append(kind)
        clean_query = query.strip().lower()
        fts_join = ""
        if clean_query:
            fts_query = clean_query.replace('"', " ").replace("'", " ").replace(":", " ").strip()
            if self._fts_available and fts_query:
                fts_join = "JOIN media_index_fts f ON f.account_id=m.account_id AND f.message_id=m.message_id"
                clauses.append("f.media_index_fts MATCH ?")
                params.append(fts_query)
            else:
                clauses.append("lower(COALESCE(t.local_title,'') || ' ' || m.original_title || ' ' || m.filename || ' ' || m.caption) LIKE ?")
                params.append(f"%{clean_query}%")
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT m.date_year,m.date_month,m.date_day,COUNT(*) AS item_count,MIN(m.message_id) AS first_message_id,MAX(m.message_id) AS last_message_id "
                "FROM media_index m LEFT JOIN media_metadata_v2 t ON t.account_id=m.account_id AND t.message_id=m.message_id "
                f"{fts_join} WHERE {' AND '.join(clauses)} "
                "GROUP BY m.date_year,m.date_month,m.date_day ORDER BY m.date_day DESC",
                params,
            )
            rows = await cursor.fetchall()
        return self._timeline_tree(rows)

    @staticmethod
    def _timeline_tree(rows: list[aiosqlite.Row]) -> list[dict[str, Any]]:
        years: dict[int, dict[str, Any]] = {}
        for row in rows:
            year = int(row["date_year"])
            month = str(row["date_month"])
            day = str(row["date_day"])
            year_item = years.setdefault(year, {"year": year, "count": 0, "months": {}})
            month_item = year_item["months"].setdefault(month, {"month": month, "count": 0, "days": []})
            day_item = {
                "day": day,
                "count": int(row["item_count"]),
                "first_message_id": int(row["first_message_id"]),
                "last_message_id": int(row["last_message_id"]),
            }
            month_item["days"].append(day_item)
            year_item["count"] += day_item["count"]
            month_item["count"] += day_item["count"]
        result: list[dict[str, Any]] = []
        for year in sorted(years, reverse=True):
            item = years[year]
            item["months"] = [item["months"][key] for key in sorted(item["months"], reverse=True)]
            result.append(item)
        return result

    async def list_media_reviews(
        self,
        *,
        status: str = "pending",
        account_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        allowed = {"pending", "approved", "rejected", "revoked", "not_required", "all"}
        if status not in allowed:
            raise ValueError("invalid review status")
        clauses = ["m.deleted=0"]
        params: list[Any] = []
        if status != "all":
            clauses.append("m.review_status=?")
            params.append(status)
        if account_id:
            clauses.append("m.account_id=?")
            params.append(account_id)
        params.append(max(1, min(1000, int(limit))))
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT m.*, t.local_title, COALESCE(NULLIF(t.local_title,''), m.original_title) AS title "
                "FROM media_index m LEFT JOIN media_metadata_v2 t "
                "ON t.account_id=m.account_id AND t.message_id=m.message_id "
                f"WHERE {' AND '.join(clauses)} ORDER BY m.indexed_at DESC,m.message_id DESC LIMIT ?",
                params,
            )
            rows = await cursor.fetchall()
        return [self._media_row(row, include_provenance=True) for row in rows]

    async def media_review_targets(
        self,
        account_id: str,
        message_id: int,
        *,
        include_batch: bool = True,
    ) -> list[dict[str, Any]]:
        """Return the live media rows affected by a review/delete action.

        Media groups are reviewed as one unit.  Keeping this lookup in the
        database layer makes the API delete path use the same target set as
        the normal review path and prevents a partial album from remaining
        visible after a policy deletion.
        """
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM media_index WHERE account_id=? AND message_id=? AND deleted=0",
                (account_id, int(message_id)),
            )
            current = await cursor.fetchone()
            if not current:
                return []
            current_dict = dict(current)
            batch = str(current_dict.get("review_batch_id") or "").strip()
            if include_batch and batch:
                cursor = await db.execute(
                    "SELECT * FROM media_index WHERE account_id=? AND review_batch_id=? AND deleted=0 ORDER BY message_id",
                    (account_id, batch),
                )
                rows = await cursor.fetchall()
            else:
                rows = [current]
        return [self._media_row(row, include_provenance=True) for row in rows]

    async def tombstone_media(
        self,
        account_id: str,
        message_id: int,
        *,
        reason: str | None = None,
        deleted_by: str = "admin",
        include_batch: bool = True,
    ) -> list[dict[str, Any]]:
        """Hide and scrub a deleted media row while retaining minimal audit data."""
        clean_reason = (reason or "违规内容").strip()[:1000] or "违规内容"
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM media_index WHERE account_id=? AND message_id=? AND deleted=0",
                (account_id, int(message_id)),
            )
            current = await cursor.fetchone()
            if not current:
                await db.rollback()
                return []
            current_dict = dict(current)
            batch = str(current_dict.get("review_batch_id") or "").strip()
            if include_batch and batch:
                cursor = await db.execute(
                    "SELECT * FROM media_index WHERE account_id=? AND review_batch_id=? AND deleted=0 ORDER BY message_id",
                    (account_id, batch),
                )
                targets = [dict(row) for row in await cursor.fetchall()]
            else:
                targets = [current_dict]
            target_ids = [int(target["message_id"]) for target in targets]
            for target in targets:
                await db.execute(
                    "INSERT INTO media_deletion_events(account_id,message_id,source_ingest_job_id,submitter_telegram_user_id,reason,deleted_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        account_id,
                        int(target["message_id"]),
                        target.get("source_ingest_job_id"),
                        target.get("submitter_telegram_user_id"),
                        clean_reason,
                        str(deleted_by),
                        now,
                    ),
                )
                await db.execute(
                    "UPDATE media_index SET kind='file',mime_type='application/octet-stream',size=0,filename='[deleted]',"
                    "original_title='[deleted]',caption='',duration=NULL,width=NULL,height=NULL,has_thumbnail=0,"
                    "visibility='private',hidden=0,requested_visibility='private',review_status='rejected',review_reason=?,"
                    "reviewed_at=?,reviewed_by=?,deleted=1,last_seen_at=? WHERE account_id=? AND message_id=?",
                    (
                        clean_reason,
                        now,
                        str(deleted_by),
                        now,
                        account_id,
                        int(target["message_id"]),
                    ),
                )
                await db.execute(
                    "DELETE FROM media_metadata_v2 WHERE account_id=? AND message_id=?",
                    (account_id, int(target["message_id"])),
                )
                await db.execute(
                    "DELETE FROM media_likes WHERE account_id=? AND message_id=?",
                    (account_id, int(target["message_id"])),
                )
                if account_id == "default":
                    await db.execute(
                        "DELETE FROM media_metadata WHERE message_id=?",
                        (int(target["message_id"]),),
                    )
                if self._fts_available:
                    await db.execute(
                        "DELETE FROM media_index_fts WHERE account_id=? AND message_id=?",
                        (account_id, int(target["message_id"])),
                    )
                job_id = target.get("source_ingest_job_id")
                if job_id is not None:
                    await db.execute("DELETE FROM review_sync_outbox WHERE job_id=?", (int(job_id),))
            await db.commit()
        await self.rebuild_timeline(account_id)
        deleted_rows: list[dict[str, Any]] = []
        for target_id in target_ids:
            row = await self.get_media_index(
                account_id,
                target_id,
                include_deleted=True,
                include_provenance=True,
            )
            if row:
                deleted_rows.append(row)
        return deleted_rows

    async def review_media(
        self,
        account_id: str,
        message_id: int,
        decision: str,
        *,
        reason: str | None = None,
        reviewed_by: str = "admin",
        review_batch_id: str | None = None,
    ) -> dict[str, Any] | None:
        if decision not in {"approved", "rejected", "revoked"}:
            raise ValueError("invalid review decision")
        clean_reason = (reason or "").strip()[:1000] or None
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM media_index WHERE account_id=? AND message_id=? AND deleted=0",
                (account_id, int(message_id)),
            )
            current = await cursor.fetchone()
            if not current:
                await db.rollback()
                return None
            current_dict = dict(current)
            new_visibility = "public" if decision == "approved" else "private"
            new_status = decision
            new_requested = "public" if decision in {"approved", "rejected", "revoked"} else current_dict.get("requested_visibility", "private")
            batch = review_batch_id or current_dict.get("review_batch_id")
            targets = [current_dict]
            if batch:
                batch_cursor = await db.execute(
                    "SELECT * FROM media_index WHERE account_id=? AND review_batch_id=? AND deleted=0",
                    (account_id, str(batch)),
                )
                targets = [dict(row) for row in await batch_cursor.fetchall()]
            changed = False
            for target in targets:
                target_changed = any(
                    [
                        target.get("visibility") != new_visibility,
                        target.get("requested_visibility") != new_requested,
                        target.get("review_status") != new_status,
                        target.get("review_reason") != clean_reason,
                        target.get("reviewed_by") != reviewed_by,
                        bool(target.get("hidden")),
                    ]
                )
                changed = changed or target_changed
                if target_changed:
                    await db.execute(
                        "UPDATE media_index SET visibility=?,requested_visibility=?,review_status=?,review_reason=?,"
                        "reviewed_at=?,reviewed_by=?,review_batch_id=?,hidden=0 WHERE account_id=? AND message_id=?",
                        (
                            new_visibility,
                            new_requested,
                            new_status,
                            clean_reason,
                            now,
                            str(reviewed_by),
                            batch,
                            account_id,
                            int(target["message_id"]),
                        ),
                    )
                    await db.execute(
                        "INSERT INTO media_review_events(account_id,message_id,review_batch_id,decision,reason,reviewed_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (account_id, int(target["message_id"]), batch, decision, clean_reason, str(reviewed_by), now),
                    )
                job_id = target.get("source_ingest_job_id")
                if job_id is not None:
                    await db.execute(
                        "INSERT INTO review_sync_outbox(job_id,account_id,decision,reason,reviewed_by,attempts,last_error,next_attempt_at,updated_at) "
                        "VALUES(?,?,?,?,?,0,NULL,?,?) "
                        "ON CONFLICT(job_id) DO UPDATE SET account_id=excluded.account_id,decision=excluded.decision,"
                        "reason=excluded.reason,reviewed_by=excluded.reviewed_by,attempts=0,last_error=NULL,"
                        "next_attempt_at=excluded.next_attempt_at,updated_at=excluded.updated_at",
                        (int(job_id), account_id, decision, clean_reason, str(reviewed_by), now, now),
                    )
            await db.commit()
        if changed:
            await self.rebuild_timeline(account_id)
        return await self.get_media_index(account_id, message_id, include_provenance=True)

    async def review_media_bulk(
        self,
        entries: Iterable[dict[str, Any]],
        decision: str,
        *,
        reason: str | None = None,
        reviewed_by: str = "admin",
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for entry in entries:
            item = await self.review_media(
                str(entry["account_id"]),
                int(entry["message_id"]),
                decision,
                reason=reason,
                reviewed_by=reviewed_by,
                review_batch_id=entry.get("review_batch_id"),
            )
            if item:
                results.append(item)
        return results

    async def list_review_sync_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM review_sync_outbox WHERE next_attempt_at<=? ORDER BY updated_at LIMIT ?",
                (now, max(1, min(500, int(limit)))),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def mark_review_sync_success(self, job_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM review_sync_outbox WHERE job_id=?", (int(job_id),))
            await db.commit()

    async def mark_review_sync_failure(self, job_id: int, error: str, *, delay_seconds: int = 30) -> None:
        next_attempt = datetime.now(timezone.utc) + timedelta(seconds=max(1, int(delay_seconds)))
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE review_sync_outbox SET attempts=attempts+1,last_error=?,next_attempt_at=?,updated_at=? WHERE job_id=?",
                (str(error)[:1000], next_attempt.isoformat(), _now(), int(job_id)),
            )
            await db.commit()

    async def set_media_visibility(self, account_id: str, message_id: int, visibility: str) -> dict[str, Any] | None:
        if visibility not in {"public", "private"}:
            raise ValueError("invalid visibility")
        review_status = "approved" if visibility == "public" else "revoked"
        requested_visibility = "public" if visibility == "public" else "private"
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "UPDATE media_index SET visibility=?,requested_visibility=?,review_status=? "
                "WHERE account_id=? AND message_id=? AND deleted=0",
                (visibility, requested_visibility, review_status, account_id, int(message_id)),
            )
            await db.commit()
            changed = cursor.rowcount
        if not changed:
            return None
        await self.rebuild_timeline(account_id)
        return await self.get_media_index(account_id, message_id)

    async def set_media_visibility_bulk(self, entries: Iterable[dict[str, Any]], visibility: str) -> int:
        if visibility not in {"public", "private"}:
            raise ValueError("invalid visibility")
        review_status = "approved" if visibility == "public" else "revoked"
        requested_visibility = "public" if visibility == "public" else "private"
        values = [(visibility, requested_visibility, review_status, str(entry["account_id"]), int(entry["message_id"])) for entry in entries]
        if not values:
            return 0
        accounts = {row[1] for row in values}
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.executemany(
                "UPDATE media_index SET visibility=?,requested_visibility=?,review_status=? "
                "WHERE account_id=? AND message_id=? AND deleted=0",
                values,
            )
            changed = int(cursor.rowcount or 0)
            await db.commit()
        for account_id in accounts:
            await self.rebuild_timeline(account_id)
        return changed

    async def mark_media_deleted(self, account_id: str, message_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE media_index SET deleted=1,last_seen_at=? WHERE account_id=? AND message_id=?",
                (_now(), account_id, int(message_id)),
            )
            await db.commit()
        await self.rebuild_timeline(account_id)

    async def mark_media_missing(self, account_id: str, seen_message_ids: Iterable[int]) -> int:
        """Mark indexed messages absent from a completed full Telegram scan."""
        seen_values: set[int] = set()
        for raw_message_id in seen_message_ids:
            try:
                message_id = int(raw_message_id)
            except (TypeError, ValueError):
                continue
            if message_id > 0:
                seen_values.add(message_id)
        seen = sorted(seen_values)
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("CREATE TEMP TABLE seen_media_ids(message_id INTEGER PRIMARY KEY)")
            if seen:
                await db.executemany(
                    "INSERT INTO seen_media_ids(message_id) VALUES(?)",
                    [(message_id,) for message_id in seen],
                )
            cursor = await db.execute(
                "UPDATE media_index SET deleted=1,last_seen_at=? "
                "WHERE account_id=? AND deleted=0 AND NOT EXISTS ("
                "  SELECT 1 FROM seen_media_ids s WHERE s.message_id=media_index.message_id"
                ")",
                (now, account_id),
            )
            changed = int(cursor.rowcount or 0)
            await db.commit()
        if changed:
            await self.rebuild_timeline(account_id)
        return changed

    async def get_sync_state(self, account_id: str) -> dict[str, Any]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM media_sync_state WHERE account_id=?", (account_id,))
            row = await cursor.fetchone()
        if row:
            return dict(row)
        return {
            "account_id": account_id,
            "status": "idle",
            "mode": "incremental",
            "cursor": None,
            "high_watermark_id": None,
            "indexed_count": 0,
            "last_sync_at": None,
            "error": None,
            "updated_at": _now(),
        }

    async def list_sync_states(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM media_sync_state ORDER BY account_id")
            return [dict(row) for row in await cursor.fetchall()]

    async def update_sync_state(self, account_id: str, **updates: Any) -> dict[str, Any]:
        allowed = {"status", "mode", "cursor", "high_watermark_id", "indexed_count", "last_sync_at", "error"}
        updates = {key: value for key, value in updates.items() if key in allowed}
        current = await self.get_sync_state(account_id)
        current.update(updates)
        current["updated_at"] = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO media_sync_state(account_id,status,mode,cursor,high_watermark_id,indexed_count,last_sync_at,error,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account_id) DO UPDATE SET
                    status=excluded.status,mode=excluded.mode,cursor=excluded.cursor,
                    high_watermark_id=excluded.high_watermark_id,indexed_count=excluded.indexed_count,
                    last_sync_at=excluded.last_sync_at,error=excluded.error,updated_at=excluded.updated_at
                """,
                (
                    account_id,
                    current["status"],
                    current["mode"],
                    current["cursor"],
                    current["high_watermark_id"],
                    current["indexed_count"],
                    current["last_sync_at"],
                    current["error"],
                    current["updated_at"],
                ),
            )
            await db.commit()
        return current

    async def get_ingest_reconcile_state(self) -> dict[str, Any]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM ingest_reconcile_state WHERE id=1")
            row = await cursor.fetchone()
        if row:
            return dict(row)
        return {
            "id": 1,
            "last_updated_at": 0,
            "last_job_id": 0,
            "last_run_at": None,
            "error": None,
            "updated_at": _now(),
        }

    async def update_ingest_reconcile_state(self, **updates: Any) -> dict[str, Any]:
        allowed = {"last_updated_at", "last_job_id", "last_run_at", "error"}
        updates = {key: value for key, value in updates.items() if key in allowed}
        current = await self.get_ingest_reconcile_state()
        current.update(updates)
        current["updated_at"] = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO ingest_reconcile_state(id,last_updated_at,last_job_id,last_run_at,error,updated_at)
                VALUES(1,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    last_updated_at=excluded.last_updated_at,
                    last_job_id=excluded.last_job_id,
                    last_run_at=excluded.last_run_at,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    int(current.get("last_updated_at") or 0),
                    int(current.get("last_job_id") or 0),
                    current.get("last_run_at"),
                    current.get("error"),
                    current["updated_at"],
                ),
            )
            await db.commit()
        return current

    async def create_upload_job(
        self,
        *,
        job_id: str,
        account_id: str,
        filename: str,
        mime_type: str,
        size: int,
        temp_path: str,
        owner_user_id: int | None = None,
        submitter_telegram_user_id: str | None = None,
        requested_visibility: str = "private",
        review_status: str = "not_required",
        batch_id: str | None = None,
        upload_source: str = "web",
        quota_reservation_key: str | None = None,
    ) -> dict[str, Any]:
        if requested_visibility not in {"public", "private"}:
            raise ValueError("invalid requested visibility")
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO upload_jobs(id,account_id,filename,mime_type,size,status,phase,progress,bytes_sent,temp_path,"
                "owner_user_id,submitter_telegram_user_id,requested_visibility,review_status,batch_id,upload_source,quota_reservation_key,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'queued','receiving',0,0,?,?,?,?,?,?,?,?, ?,?)",
                (
                    job_id,
                    account_id,
                    filename,
                    mime_type,
                    int(size),
                    temp_path,
                    int(owner_user_id) if owner_user_id is not None else None,
                    str(submitter_telegram_user_id) if submitter_telegram_user_id else None,
                    requested_visibility,
                    review_status,
                    batch_id,
                    str(upload_source)[:40],
                    quota_reservation_key,
                    now,
                    now,
                ),
            )
            await db.commit()
        return await self.get_upload_job(job_id)  # type: ignore[return-value]

    async def update_upload_job(self, job_id: str, **updates: Any) -> dict[str, Any] | None:
        allowed = {"status", "phase", "progress", "bytes_sent", "message_id", "error", "temp_path", "quota_reservation_key"}
        updates = {key: value for key, value in updates.items() if key in allowed}
        if not updates:
            return await self.get_upload_job(job_id)
        assignments = ",".join(f"{key}=?" for key in updates)
        values = [updates[key] for key in updates]
        values.extend([_now(), job_id])
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"UPDATE upload_jobs SET {assignments},updated_at=? WHERE id=?", values)
            await db.commit()
        return await self.get_upload_job(job_id)

    async def cancel_upload_job(self, job_id: str) -> dict[str, Any] | None:
        """Atomically request cancellation for a non-terminal upload.

        The worker may be uploading to TeleBox concurrently.  Keeping the
        transition conditional prevents a late DELETE request from changing a
        completed/failed job, while the worker checks the cancelled state
        before it writes the Telegram message into the local index.
        """
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE upload_jobs SET status='cancelled', phase='cancelled', "
                "error='upload cancelled', temp_path=NULL, updated_at=? "
                "WHERE id=? AND status NOT IN ('completed','failed','cancelled')",
                (now, job_id),
            )
            await db.commit()
        return await self.get_upload_job(job_id)

    async def complete_upload_job(
        self,
        job_id: str,
        *,
        message_id: int,
    ) -> dict[str, Any] | None:
        """Complete an upload unless an administrator cancelled it first."""
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE upload_jobs SET status='completed', phase='completed', "
                "progress=100, bytes_sent=size, message_id=?, error=NULL, "
                "temp_path=NULL, updated_at=? "
                "WHERE id=? AND status NOT IN ('cancelled','failed')",
                (int(message_id), now, job_id),
            )
            await db.commit()
        return await self.get_upload_job(job_id)

    async def get_upload_job(self, job_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM upload_jobs WHERE id=?", (job_id,))
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_upload_jobs(self, limit: int = 100, *, owner_user_id: int | None = None) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            if owner_user_id is None:
                cursor = await db.execute("SELECT * FROM upload_jobs ORDER BY created_at DESC LIMIT ?", (int(limit),))
            else:
                cursor = await db.execute(
                    "SELECT * FROM upload_jobs WHERE owner_user_id=? ORDER BY created_at DESC LIMIT ?",
                    (int(owner_user_id), int(limit)),
                )
            return [dict(row) for row in await cursor.fetchall()]

    # ------------------------------------------------------------------
    # Public square: likes, reports, sanctions, and moderation jobs
    # ------------------------------------------------------------------

    async def media_social_state(self, account_id: str, message_id: int, viewer_user_id: int | None) -> dict[str, Any]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT COUNT(*) AS like_count, "
                "EXISTS(SELECT 1 FROM media_likes WHERE user_id=? AND account_id=? AND message_id=?) AS liked_by_me "
                "FROM media_likes WHERE account_id=? AND message_id=?",
                (int(viewer_user_id or -1), account_id, int(message_id), account_id, int(message_id)),
            )
            row = await cursor.fetchone()
        return {
            "like_count": int(row["like_count"] if row else 0),
            "liked_by_me": bool(row["liked_by_me"] if row else 0),
        }

    async def set_media_like(self, user_id: int, account_id: str, message_id: int, liked: bool) -> dict[str, Any]:
        async with aiosqlite.connect(self.path) as db:
            if liked:
                await db.execute(
                    "INSERT OR IGNORE INTO media_likes(user_id,account_id,message_id,created_at) VALUES(?,?,?,?)",
                    (int(user_id), account_id, int(message_id), _now()),
                )
            else:
                await db.execute(
                    "DELETE FROM media_likes WHERE user_id=? AND account_id=? AND message_id=?",
                    (int(user_id), account_id, int(message_id)),
                )
            await db.commit()
        return await self.media_social_state(account_id, message_id, user_id)

    async def create_media_report(
        self,
        *,
        reporter_user_id: int,
        account_id: str,
        message_id: int,
        owner_user_id: int | None,
        reason_code: str,
        details: str | None,
        media_title: str,
    ) -> dict[str, Any]:
        now = _now()
        try:
            async with aiosqlite.connect(self.path) as db:
                # Keep the duplicate check and insert under one write lock so
                # a reporter cannot create a second unfinished report while
                # the first one is processing or waiting for a failed action
                # to be retried.
                await db.execute("BEGIN IMMEDIATE")
                existing = await db.execute(
                    "SELECT id FROM media_reports WHERE reporter_user_id=? AND account_id=? AND message_id=? "
                    "AND status IN ('open','processing','failed') LIMIT 1",
                    (int(reporter_user_id), account_id, int(message_id)),
                )
                if await existing.fetchone():
                    await db.rollback()
                    raise ValueError("duplicate unfinished report")
                cursor = await db.execute(
                    "INSERT INTO media_reports(reporter_user_id,account_id,message_id,owner_user_id,reason_code,details,media_title,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,'open',?)",
                    (
                        int(reporter_user_id),
                        account_id,
                        int(message_id),
                        int(owner_user_id) if owner_user_id is not None else None,
                        str(reason_code)[:40],
                        (details or "").strip()[:1000] or None,
                        str(media_title)[:200],
                        now,
                    ),
                )
                report_id = int(cursor.lastrowid)
                await db.commit()
        except ValueError:
            raise
        except (sqlite3.IntegrityError, aiosqlite.IntegrityError) as exc:
            raise ValueError("duplicate open report") from exc
        report = await self.get_media_report(report_id)
        assert report is not None
        return report

    async def get_media_report(self, report_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT r.*,COALESCE(ru.username_display,ru.username_normalized,ru.display_name,'#' || r.reporter_user_id) AS reporter_name,"
                "COALESCE(ou.username_display,ou.username_normalized,ou.display_name,'') AS owner_name "
                "FROM media_reports r LEFT JOIN auth_users ru ON ru.id=r.reporter_user_id "
                "LEFT JOIN auth_users ou ON ou.id=r.owner_user_id WHERE r.id=?",
                (int(report_id),),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_media_reports(self, *, status: str = "open", limit: int = 200) -> list[dict[str, Any]]:
        if status not in {"open", "actionable", "processing", "resolved", "ignored", "failed", "all"}:
            raise ValueError("invalid report status")
        clauses: list[str] = []
        params: list[Any] = []
        if status == "actionable":
            clauses.append("r.status IN ('open','failed')")
        elif status != "all":
            clauses.append("r.status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(1000, int(limit))))
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT r.*,COALESCE(ru.username_display,ru.username_normalized,ru.display_name,'#' || r.reporter_user_id) AS reporter_name,"
                "COALESCE(ou.username_display,ou.username_normalized,ou.display_name,'') AS owner_name,"
                "m.visibility,m.hidden,m.deleted,m.review_status "
                "FROM media_reports r LEFT JOIN auth_users ru ON ru.id=r.reporter_user_id "
                "LEFT JOIN auth_users ou ON ou.id=r.owner_user_id "
                "LEFT JOIN media_index m ON m.account_id=r.account_id AND m.message_id=r.message_id "
                f"{where} ORDER BY r.id DESC LIMIT ?",
                params,
            )
            rows = [dict(row) for row in await cursor.fetchall()]
        groups: dict[tuple[str, int], dict[str, Any]] = {}
        for row in rows:
            key = (str(row["account_id"]), int(row["message_id"]))
            group = groups.setdefault(
                key,
                {
                    "account_id": key[0],
                    "message_id": key[1],
                    "media_title": row.get("media_title"),
                    "owner_user_id": row.get("owner_user_id"),
                    "owner_name": row.get("owner_name"),
                    "visibility": "hidden" if row.get("hidden") else row.get("visibility"),
                    "deleted": bool(row.get("deleted")),
                    "review_status": row.get("review_status"),
                    "reports": [],
                    "report_count": 0,
                },
            )
            group["reports"].append(row)
            group["report_count"] += 1
        return list(groups.values())

    async def resolve_media_reports(
        self,
        account_id: str,
        message_id: int,
        *,
        status: str,
        action: str,
        reason: str | None,
        resolved_by: int | None,
    ) -> list[dict[str, Any]]:
        if status not in {"resolved", "ignored", "failed", "processing"}:
            raise ValueError("invalid report resolution status")
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM media_reports WHERE account_id=? AND message_id=? AND status IN ('open','processing','failed') ORDER BY id",
                (account_id, int(message_id)),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            if rows:
                await db.execute(
                    "UPDATE media_reports SET status=?,resolution_action=?,resolution_reason=?,resolved_by=?,resolved_at=? "
                    "WHERE account_id=? AND message_id=? AND status IN ('open','processing','failed')",
                    (
                        status,
                        str(action)[:40],
                        (reason or "").strip()[:1000] or None,
                        int(resolved_by) if resolved_by is not None else None,
                        now if status in {"resolved", "ignored"} else None,
                        account_id,
                        int(message_id),
                    ),
                )
                await db.commit()
        return rows

    async def create_user_sanction(
        self,
        *,
        user_id: int,
        sanction_type: str,
        reason: str,
        expires_at: str | None,
        created_by: int | None,
    ) -> dict[str, Any]:
        if sanction_type not in {"upload_mute", "login_ban", "report_mute"}:
            raise ValueError("invalid sanction type")
        now = _now()
        clean_reason = reason.strip()[:1000] or "违反平台规则"
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE user_sanctions SET revoked_at=?,revoked_by=? WHERE user_id=? AND sanction_type=? "
                "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)",
                (now, int(created_by) if created_by is not None else None, int(user_id), sanction_type, now),
            )
            cursor = await db.execute(
                "INSERT INTO user_sanctions(user_id,sanction_type,reason,starts_at,expires_at,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    int(user_id),
                    sanction_type,
                    clean_reason,
                    now,
                    expires_at,
                    int(created_by) if created_by is not None else None,
                    now,
                ),
            )
            sanction_id = int(cursor.lastrowid)
            await db.commit()
        item = await self.get_user_sanction(sanction_id)
        assert item is not None
        return item

    async def get_user_sanction(self, sanction_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM user_sanctions WHERE id=?", (int(sanction_id),))
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_user_sanctions(self, user_id: int, *, active_only: bool = False) -> list[dict[str, Any]]:
        clauses = ["user_id=?"]
        params: list[Any] = [int(user_id)]
        if active_only:
            clauses.extend(["revoked_at IS NULL", "(expires_at IS NULL OR expires_at>?)"])
            params.append(_now())
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM user_sanctions WHERE {' AND '.join(clauses)} ORDER BY id DESC",
                params,
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def active_user_sanction(self, user_id: int, sanction_types: Iterable[str]) -> dict[str, Any] | None:
        types = [str(item) for item in sanction_types]
        if not types:
            return None
        placeholders = ",".join("?" for _ in types)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM user_sanctions WHERE user_id=? AND sanction_type IN ({placeholders}) "
                "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?) ORDER BY id DESC LIMIT 1",
                [int(user_id), *types, _now()],
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def revoke_user_sanction(self, sanction_id: int, *, revoked_by: int | None) -> dict[str, Any] | None:
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE user_sanctions SET revoked_at=?,revoked_by=? WHERE id=? AND revoked_at IS NULL",
                (now, int(revoked_by) if revoked_by is not None else None, int(sanction_id)),
            )
            await db.commit()
        return await self.get_user_sanction(sanction_id)

    async def create_content_deletion_job(
        self,
        *,
        job_id: str,
        target_user_id: int,
        telegram_user_id: str | None,
        reason: str,
        created_by: int | None,
    ) -> dict[str, Any]:
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT account_id,message_id FROM media_index WHERE deleted=0 AND "
                "(owner_user_id=? OR (owner_user_id IS NULL AND submitter_telegram_user_id=?)) "
                "AND (upload_source IN ('web','helper_bot') OR source_ingest_job_id IS NOT NULL)",
                (int(target_user_id), str(telegram_user_id or "")),
            )
            targets = [(str(row[0]), int(row[1])) for row in await cursor.fetchall()]
            await db.execute(
                "INSERT INTO content_deletion_jobs(id,target_user_id,reason,status,total_items,created_by,created_at,updated_at) "
                "VALUES(?,?,?,'queued',?,?,?,?)",
                (
                    job_id,
                    int(target_user_id),
                    reason.strip()[:1000] or "管理员删除全部归属内容",
                    len(targets),
                    int(created_by) if created_by is not None else None,
                    now,
                    now,
                ),
            )
            if targets:
                await db.executemany(
                    "INSERT INTO content_deletion_job_items(job_id,account_id,message_id,status,updated_at) VALUES(?,?,?,'pending',?)",
                    [(job_id, account_id, message_id, now) for account_id, message_id in targets],
                )
            await db.commit()
        job = await self.get_content_deletion_job(job_id)
        assert job is not None
        return job

    async def get_content_deletion_job(self, job_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM content_deletion_jobs WHERE id=?", (job_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            item = dict(row)
            cursor = await db.execute(
                "SELECT account_id,message_id,status,error,updated_at FROM content_deletion_job_items WHERE job_id=? ORDER BY account_id,message_id",
                (job_id,),
            )
            item["items"] = [dict(child) for child in await cursor.fetchall()]
            return item

    async def list_content_deletion_jobs(self, *, target_user_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            if target_user_id is None:
                cursor = await db.execute(
                    "SELECT * FROM content_deletion_jobs ORDER BY created_at DESC LIMIT ?", (int(limit),)
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM content_deletion_jobs WHERE target_user_id=? ORDER BY created_at DESC LIMIT ?",
                    (int(target_user_id), int(limit)),
                )
            return [dict(row) for row in await cursor.fetchall()]

    async def pending_content_deletion_items(self, job_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM content_deletion_job_items WHERE job_id=? AND status IN ('pending','failed') ORDER BY account_id,message_id",
                (job_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def update_content_deletion_item(self, job_id: str, account_id: str, message_id: int, *, status: str, error: str | None = None) -> None:
        if status not in {"pending", "completed", "failed"}:
            raise ValueError("invalid deletion item status")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE content_deletion_job_items SET status=?,error=?,updated_at=? WHERE job_id=? AND account_id=? AND message_id=?",
                (status, (error or "")[:1000] or None, _now(), job_id, account_id, int(message_id)),
            )
            await db.commit()

    async def refresh_content_deletion_job(
        self,
        job_id: str,
        *,
        running: bool = False,
        error: str | None = None,
        cancelled: bool = False,
    ) -> dict[str, Any] | None:
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*),SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) "
                "FROM content_deletion_job_items WHERE job_id=?",
                (job_id,),
            )
            row = await cursor.fetchone()
            total = int(row[0] or 0) if row else 0
            completed = int(row[1] or 0) if row else 0
            failed = int(row[2] or 0) if row else 0
            pending = int(row[3] or 0) if row else 0
            if cancelled:
                status = "cancelled"
                completed_at = now
            elif running:
                status = "running"
                completed_at = None
            elif failed and completed:
                status = "partial"
                completed_at = now
            elif failed:
                status = "failed"
                completed_at = now
            elif pending:
                # An outer worker failure/shutdown must never turn untouched
                # Telegram messages into a falsely completed deletion job.
                status = "failed"
                completed_at = now
            else:
                status = "completed"
                completed_at = now
            await db.execute(
                "UPDATE content_deletion_jobs SET status=?,total_items=?,processed_items=?,failed_items=?,error=?,updated_at=?,completed_at=? WHERE id=?",
                (status, total, completed + failed, failed, (error or "")[:1000] or None, now, completed_at, job_id),
            )
            await db.commit()
        return await self.get_content_deletion_job(job_id)

    async def get_traffic_settings(self) -> dict[str, Any]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM traffic_limit_settings WHERE id=1")
            row = await cursor.fetchone()
        if not row:
            return {
                "enabled": 0,
                "monthly_capacity_bytes": DEFAULT_TRAFFIC_CAPACITY_BYTES,
                "monthly_limit_bytes": DEFAULT_TRAFFIC_LIMIT_BYTES,
                "warning_percent": 80,
                "admin_bypass": 0,
                "timezone": "UTC",
            }
        return dict(row)

    async def set_traffic_settings(
        self,
        *,
        enabled: bool,
        monthly_capacity_bytes: int,
        monthly_limit_bytes: int,
        warning_percent: int,
        admin_bypass: bool,
        timezone_name: str = "UTC",
    ) -> dict[str, Any]:
        if monthly_capacity_bytes <= 0 or monthly_limit_bytes <= 0:
            raise ValueError("traffic limits must be positive")
        if monthly_limit_bytes > monthly_capacity_bytes:
            raise ValueError("monthly traffic limit cannot exceed capacity")
        if not 1 <= warning_percent <= 99:
            raise ValueError("warning percent must be between 1 and 99")
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO traffic_limit_settings("
                "id,enabled,monthly_capacity_bytes,monthly_limit_bytes,warning_percent,admin_bypass,timezone,updated_at) "
                "VALUES(1,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled, "
                "monthly_capacity_bytes=excluded.monthly_capacity_bytes, "
                "monthly_limit_bytes=excluded.monthly_limit_bytes, "
                "warning_percent=excluded.warning_percent, admin_bypass=excluded.admin_bypass, "
                "timezone=excluded.timezone, updated_at=excluded.updated_at",
                (
                    1 if enabled else 0,
                    int(monthly_capacity_bytes),
                    int(monthly_limit_bytes),
                    int(warning_percent),
                    1 if admin_bypass else 0,
                    timezone_name or "UTC",
                    now,
                ),
            )
            await db.commit()
        return await self.get_traffic_settings()

    @staticmethod
    def _traffic_periods(now: datetime | None = None) -> tuple[str, str]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return current.strftime("%Y-%m-%d"), current.strftime("%Y-%m")

    async def consume_traffic(
        self,
        direction: str,
        amount: int,
        *,
        request_count: int = 0,
        bypass_limit: bool = False,
    ) -> dict[str, Any]:
        """Atomically account for transfer bytes and enforce the monthly cap.

        Bytes are accounted at the SavedStream boundary only.  Telegram's
        internal transfer is intentionally not included in this counter.
        The update happens before a response chunk is yielded so concurrent
        streams cannot collectively pass the configured hard limit.
        """
        if direction not in {"in", "out"}:
            raise ValueError("traffic direction must be 'in' or 'out'")
        amount = max(0, int(amount))
        request_count = max(0, int(request_count))
        day_bucket, month_bucket = self._traffic_periods()
        now = _now()
        bytes_column = "bytes_in" if direction == "in" else "bytes_out"

        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            settings_cursor = await db.execute("SELECT * FROM traffic_limit_settings WHERE id=1")
            limit_settings = await settings_cursor.fetchone()
            if not limit_settings:
                limit_settings = {
                    "enabled": 0,
                    "monthly_limit_bytes": DEFAULT_TRAFFIC_LIMIT_BYTES,
                    "admin_bypass": 0,
                }
            else:
                limit_settings = dict(limit_settings)

            await db.executemany(
                "INSERT OR IGNORE INTO traffic_usage_buckets("
                "bucket_type,bucket_start,bytes_in,bytes_out,request_count,updated_at) "
                "VALUES(?,?,0,0,0,?)",
                [("day", day_bucket, now), ("month", month_bucket, now)],
            )
            month_cursor = await db.execute(
                "SELECT bytes_in,bytes_out,request_count FROM traffic_usage_buckets "
                "WHERE bucket_type='month' AND bucket_start=?",
                (month_bucket,),
            )
            month_row = await month_cursor.fetchone()
            used_before = int(month_row["bytes_in"]) + int(month_row["bytes_out"]) if month_row else 0
            limit_enabled = bool(int(limit_settings["enabled"])) and not (
                bypass_limit and bool(int(limit_settings.get("admin_bypass", 0)))
            )
            monthly_limit = int(limit_settings["monthly_limit_bytes"])
            allowed = not limit_enabled or amount == 0 or used_before + amount <= monthly_limit
            if allowed and (amount or request_count):
                await db.execute(
                    f"UPDATE traffic_usage_buckets SET {bytes_column}={bytes_column}+?, "
                    "request_count=request_count+?,updated_at=? WHERE bucket_type='day' AND bucket_start=?",
                    (amount, request_count, now, day_bucket),
                )
                await db.execute(
                    f"UPDATE traffic_usage_buckets SET {bytes_column}={bytes_column}+?, "
                    "request_count=request_count+?,updated_at=? WHERE bucket_type='month' AND bucket_start=?",
                    (amount, request_count, now, month_bucket),
                )
            await db.commit()

        used_after = used_before + amount if allowed else used_before
        remaining = max(0, monthly_limit - used_after)
        return {
            "allowed": allowed,
            "enabled": limit_enabled,
            "used_bytes": used_after,
            "remaining_bytes": remaining,
            "monthly_limit_bytes": monthly_limit,
            "monthly_capacity_bytes": int(limit_settings.get("monthly_capacity_bytes", DEFAULT_TRAFFIC_CAPACITY_BYTES)),
            "warning_percent": int(limit_settings.get("warning_percent", 80)),
        }

    async def get_traffic_usage(self, bucket_type: str = "month", bucket_start: str | None = None) -> dict[str, Any]:
        if bucket_type not in {"day", "month"}:
            raise ValueError("invalid traffic bucket type")
        day_bucket, month_bucket = self._traffic_periods()
        selected = bucket_start or (month_bucket if bucket_type == "month" else day_bucket)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT bucket_type,bucket_start,bytes_in,bytes_out,request_count,updated_at "
                "FROM traffic_usage_buckets WHERE bucket_type=? AND bucket_start=?",
                (bucket_type, selected),
            )
            row = await cursor.fetchone()
        if not row:
            return {
                "bucket_type": bucket_type,
                "bucket_start": selected,
                "bytes_in": 0,
                "bytes_out": 0,
                "request_count": 0,
                "updated_at": None,
            }
        return dict(row)

    async def list_traffic_series(self, range_name: str = "7d") -> list[dict[str, Any]]:
        if range_name not in {"7d", "30d", "month"}:
            raise ValueError("invalid traffic series range")
        today = datetime.now(timezone.utc).date()
        if range_name == "month":
            start = today.replace(day=1)
        else:
            days = 6 if range_name == "7d" else 29
            start = today - timedelta(days=days)
        end = today
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT bucket_start,bytes_in,bytes_out,request_count FROM traffic_usage_buckets "
                "WHERE bucket_type='day' AND bucket_start>=? AND bucket_start<=? ORDER BY bucket_start",
                (start.isoformat(), end.isoformat()),
            )
            rows = {str(row["bucket_start"]): dict(row) for row in await cursor.fetchall()}
        series: list[dict[str, Any]] = []
        current = start
        while current <= end:
            key = current.isoformat()
            row = rows.get(key, {})
            bytes_in = int(row.get("bytes_in", 0))
            bytes_out = int(row.get("bytes_out", 0))
            series.append(
                {
                    "bucket_start": key,
                    "bytes_in": bytes_in,
                    "bytes_out": bytes_out,
                    "bytes_total": bytes_in + bytes_out,
                    "request_count": int(row.get("request_count", 0)),
                }
            )
            current += timedelta(days=1)
        return series

    async def reset_traffic_usage(self, scope: str = "month") -> None:
        if scope not in {"month", "all"}:
            raise ValueError("invalid traffic reset scope")
        _, month_bucket = self._traffic_periods()
        async with aiosqlite.connect(self.path) as db:
            if scope == "all":
                await db.execute("DELETE FROM traffic_usage_buckets")
            else:
                await db.execute(
                    "DELETE FROM traffic_usage_buckets WHERE bucket_type='month' AND bucket_start=?",
                    (month_bucket,),
                )
                await db.execute(
                    "DELETE FROM traffic_usage_buckets WHERE bucket_type='day' AND bucket_start LIKE ?",
                    (f"{month_bucket}-%",),
                )
            await db.commit()

    # ------------------------------------------------------------------
    # Hidden visibility (administrator-only rows inside the media index)
    # ------------------------------------------------------------------

    async def set_media_hidden(self, account_id: str, message_id: int, hidden: bool) -> dict[str, Any] | None:
        """Hide a media row from everyone except administrators.

        Hiding forces the row back to private.  Unhiding restores normal
        private visibility; it never re-publishes media on its own.
        """
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "UPDATE media_index SET hidden=?, visibility='private', last_seen_at=? "
                "WHERE account_id=? AND message_id=? AND deleted=0",
                (1 if hidden else 0, _now(), account_id, int(message_id)),
            )
            await db.commit()
            changed = int(cursor.rowcount or 0)
        if not changed:
            return None
        await self.rebuild_timeline(account_id)
        return await self.get_media_index(account_id, message_id)

    async def set_media_hidden_bulk(self, entries: Iterable[dict[str, Any]], hidden: bool) -> int:
        values = [
            (1 if hidden else 0, _now(), str(entry["account_id"]), int(entry["message_id"]))
            for entry in entries
        ]
        if not values:
            return 0
        accounts = {row[2] for row in values}
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.executemany(
                "UPDATE media_index SET hidden=?, visibility='private', last_seen_at=? "
                "WHERE account_id=? AND message_id=? AND deleted=0",
                values,
            )
            changed = int(cursor.rowcount or 0)
            await db.commit()
        for account_id in accounts:
            await self.rebuild_timeline(account_id)
        return changed

    # ------------------------------------------------------------------
    # Folders (multi-level organization of indexed media)
    # ------------------------------------------------------------------

    async def get_folder(self, folder_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM media_folders WHERE id=?", (int(folder_id),)
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def create_folder(self, name: str, parent_id: int = 0, created_by: str | None = None) -> dict[str, Any]:
        clean_name = (name or "").strip()[:120]
        if not clean_name:
            raise ValueError("folder name must not be empty")
        now = _now()
        try:
            async with aiosqlite.connect(self.path) as db:
                if int(parent_id):
                    cursor = await db.execute("SELECT 1 FROM media_folders WHERE id=?", (int(parent_id),))
                    if not await cursor.fetchone():
                        raise ValueError("parent folder does not exist")
                cursor = await db.execute(
                    "INSERT INTO media_folders(parent_id,name,created_by,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (int(parent_id), clean_name, created_by, now, now),
                )
                folder_id = int(cursor.lastrowid)
                await db.commit()
        except aiosqlite.IntegrityError:
            raise ValueError("a folder with this name already exists in this location") from None
        return await self.get_folder(folder_id)  # type: ignore[return-value]

    async def rename_folder(self, folder_id: int, name: str) -> dict[str, Any] | None:
        clean_name = (name or "").strip()[:120]
        if not clean_name:
            raise ValueError("folder name must not be empty")
        try:
            async with aiosqlite.connect(self.path) as db:
                cursor = await db.execute(
                    "UPDATE media_folders SET name=?, updated_at=? WHERE id=?",
                    (clean_name, _now(), int(folder_id)),
                )
                await db.commit()
                if int(cursor.rowcount or 0) == 0:
                    return None
        except aiosqlite.IntegrityError:
            raise ValueError("a folder with this name already exists in this location") from None
        return await self.get_folder(folder_id)

    async def move_folder(self, folder_id: int, parent_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            check = await db.execute("SELECT 1 FROM media_folders WHERE id=?", (int(folder_id),))
            if not await check.fetchone():
                return None
            # Cycle prevention: the new parent must not be the folder itself
            # or one of its descendants.
            current = int(parent_id)
            while current:
                if current == int(folder_id):
                    raise ValueError("cannot move a folder into itself or its subfolder")
                row = await (await db.execute("SELECT parent_id FROM media_folders WHERE id=?", (current,))).fetchone()
                current = int(row[0]) if row else 0
            await db.execute(
                "UPDATE media_folders SET parent_id=?, updated_at=? WHERE id=?",
                (int(parent_id), _now(), int(folder_id)),
            )
            await db.commit()
        return await self.get_folder(folder_id)

    async def delete_folder(self, folder_id: int) -> int:
        """Delete a folder, its subfolders, and their media entries."""
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "WITH RECURSIVE subtree(id) AS ("
                " SELECT ? UNION ALL SELECT f.id FROM media_folders f JOIN subtree s ON f.parent_id=s.id"
                ") SELECT id FROM subtree",
                (int(folder_id),),
            )
            ids = [int(row[0]) for row in await cursor.fetchall()]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            await db.execute(f"DELETE FROM media_folder_items WHERE folder_id IN ({placeholders})", ids)
            cursor = await db.execute(f"DELETE FROM media_folders WHERE id IN ({placeholders})", ids)
            await db.commit()
            return int(cursor.rowcount or 0)

    async def list_folders(
        self,
        *,
        owner_telegram_user_id: str | None = None,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        """List folders with per-item counts limited to what the caller may see."""
        count_condition = ""
        count_params: list[Any] = []
        if owner_telegram_user_id:
            count_condition = (
                " AND ((m.visibility='public' AND m.review_status='approved' AND m.hidden=0) "
                "OR m.submitter_telegram_user_id=?)"
            )
            count_params.append(str(owner_telegram_user_id))
        elif not include_hidden:
            count_condition = " AND m.hidden=0"
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT f.id,f.parent_id,f.name,f.created_by,f.created_at,f.updated_at,"
                " (SELECT COUNT(*) FROM media_folder_items fi "
                "  JOIN media_index m ON m.account_id=fi.account_id AND m.message_id=fi.message_id "
                "  WHERE fi.folder_id=f.id AND m.deleted=0"
                + count_condition
                + ") AS item_count "
                "FROM media_folders f ORDER BY f.parent_id, f.name",
                count_params,
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def set_folder_items(self, folder_id: int, entries: Iterable[dict[str, Any]]) -> int:
        """Add media to a folder.  A file can live in several folders."""
        if not await self.get_folder(folder_id):
            raise ValueError("folder does not exist")
        values = [
            (int(folder_id), str(entry["account_id"]), int(entry["message_id"]), _now())
            for entry in entries
        ]
        if not values:
            return 0
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.executemany(
                "INSERT OR IGNORE INTO media_folder_items(folder_id,account_id,message_id,created_at) VALUES(?,?,?,?)",
                values,
            )
            await db.commit()
            return int(cursor.rowcount or 0)

    async def remove_folder_items(self, folder_id: int, entries: Iterable[dict[str, Any]]) -> int:
        values = [
            (int(folder_id), str(entry["account_id"]), int(entry["message_id"]))
            for entry in entries
        ]
        if not values:
            return 0
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.executemany(
                "DELETE FROM media_folder_items WHERE folder_id=? AND account_id=? AND message_id=?",
                values,
            )
            await db.commit()
            return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------
    # Notifications (per-user mailbox)
    # ------------------------------------------------------------------

    async def create_notification(
        self,
        user_id: int,
        kind: str,
        title: str,
        body: str,
        link: str | None = None,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "INSERT INTO notifications(user_id,kind,title,body,link,is_read,created_at) VALUES(?,?,?,?,?,0,?)",
                (int(user_id), str(kind)[:40], str(title)[:200], str(body)[:2000], link, _now()),
            )
            notification_id = int(cursor.lastrowid)
            await db.commit()
        return await self.get_notification(notification_id)

    async def get_notification(self, notification_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM notifications WHERE id=?", (int(notification_id),))
            row = await cursor.fetchone()
        return self._notification_row(row) if row else None

    @staticmethod
    def _notification_row(row: aiosqlite.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        raw = dict(row)
        raw["id"] = int(raw["id"])
        raw["user_id"] = int(raw["user_id"])
        raw["is_read"] = bool(raw["is_read"])
        return raw

    async def create_notification_for_telegram_user(
        self,
        telegram_user_id: str,
        kind: str,
        title: str,
        body: str,
        link: str | None = None,
    ) -> dict[str, Any] | None:
        """Deliver a notification to the web account bound to a Telegram user."""
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT id FROM auth_users WHERE telegram_user_id=?", (str(telegram_user_id),)
            )
            row = await cursor.fetchone()
        if not row:
            return None
        return await self.create_notification(int(row[0]), kind, title, body, link)

    async def create_notification_broadcast(
        self,
        kind: str,
        title: str,
        body: str,
        link: str | None = None,
    ) -> int:
        """Send a notification to every registered web account."""
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "INSERT INTO notifications(user_id,kind,title,body,link,is_read,created_at) "
                "SELECT id,?,?,?,?,0,? FROM auth_users",
                (str(kind)[:40], str(title)[:200], str(body)[:2000], link, _now()),
            )
            await db.commit()
            return int(cursor.rowcount or 0)

    async def list_notifications(
        self,
        user_id: int,
        *,
        limit: int = 30,
        cursor: int | None = None,
    ) -> tuple[list[dict[str, Any]], int | None, bool]:
        clauses = ["user_id=?"]
        params: list[Any] = [int(user_id)]
        if cursor is not None:
            clauses.append("id<?")
            params.append(int(cursor))
        params.append(max(1, min(100, int(limit))) + 1)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor_obj = await db.execute(
                f"SELECT * FROM notifications WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
                params,
            )
            rows = await cursor_obj.fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items: list[dict[str, Any]] = []
        for row in rows:
            item = self._notification_row(row)
            if item:
                items.append(item)
        next_cursor = items[-1]["id"] if has_more and items else None
        return items, next_cursor, has_more

    async def unread_notification_count(self, user_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (int(user_id),)
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def mark_notifications_read(self, user_id: int, ids: list[int] | None = None) -> int:
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            if ids:
                placeholders = ",".join("?" for _ in ids)
                cursor = await db.execute(
                    f"UPDATE notifications SET is_read=1, read_at=? WHERE user_id=? AND id IN ({placeholders}) AND is_read=0",
                    [now, int(user_id), *[int(item) for item in ids]],
                )
            else:
                cursor = await db.execute(
                    "UPDATE notifications SET is_read=1, read_at=? WHERE user_id=? AND is_read=0",
                    (now, int(user_id)),
                )
            await db.commit()
            return int(cursor.rowcount or 0)

    async def delete_notifications(self, user_id: int, ids: list[int] | None = None) -> int:
        async with aiosqlite.connect(self.path) as db:
            if ids:
                placeholders = ",".join("?" for _ in ids)
                cursor = await db.execute(
                    f"DELETE FROM notifications WHERE user_id=? AND id IN ({placeholders})",
                    [int(user_id), *[int(item) for item in ids]],
                )
            else:
                cursor = await db.execute("DELETE FROM notifications WHERE user_id=?", (int(user_id),))
            await db.commit()
            return int(cursor.rowcount or 0)

    async def list_notifications_admin(self, limit: int = 100) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT n.*, COALESCE(u.username_display, u.username_normalized, 'Telegram ' || u.telegram_user_id, '#' || n.user_id) AS recipient "
                "FROM notifications n LEFT JOIN auth_users u ON u.id=n.user_id "
                "ORDER BY n.id DESC LIMIT ?",
                (max(1, min(500, int(limit))),),
            )
            rows = await cursor.fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = self._notification_row(row)
            if item:
                item["recipient"] = str(row["recipient"])
                items.append(item)
        return items

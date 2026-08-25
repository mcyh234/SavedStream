from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

try:  # croniter is optional for local/unit environments before deps install.
    from croniter import croniter
except Exception:  # pragma: no cover - exercised only in dependency-less envs
    croniter = None


FORMAT_VERSION = 1
BACKUP_MARKER = "#savedstream-system-backup:v1"
BACKUP_FILENAME_RE = re.compile(r"^savedstream-system-\d{8}-\d{6}\.ssbak$")


class SystemBackupError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def backup_filename(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    return f"savedstream-system-{value.strftime('%Y%m%d-%H%M%S')}.ssbak"


def _derive_key(password: str, salt: bytes) -> bytes:
    if not password:
        raise SystemBackupError("backup passphrase is required")
    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(password.encode("utf-8"))


def wrap_passphrase(passphrase: str, admin_key: str) -> dict[str, str]:
    if not admin_key:
        raise SystemBackupError("ADMIN_KEY is required to persist the backup passphrase")
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(admin_key, salt)
    ciphertext = AESGCM(key).encrypt(nonce, passphrase.encode("utf-8"), b"savedstream-backup-passphrase")
    return {
        "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
    }


def unwrap_passphrase(wrapped: dict[str, str], admin_key: str) -> str:
    try:
        salt = base64.urlsafe_b64decode(wrapped["salt"])
        nonce = base64.urlsafe_b64decode(wrapped["nonce"])
        ciphertext = base64.urlsafe_b64decode(wrapped["ciphertext"])
        key = _derive_key(admin_key, salt)
        return AESGCM(key).decrypt(nonce, ciphertext, b"savedstream-backup-passphrase").decode("utf-8")
    except Exception as exc:
        raise SystemBackupError("stored backup passphrase cannot be decrypted") from exc


def _safe_extract_tar(tar: tarfile.TarFile, destination: Path) -> None:
    root = destination.resolve()
    for member in tar.getmembers():
        candidate = (root / member.name).resolve()
        if candidate != root and not str(candidate).startswith(str(root) + os.sep):
            raise SystemBackupError("backup contains an unsafe path")
        if member.issym() or member.islnk():
            raise SystemBackupError("backup links are not allowed")
    tar.extractall(root)


def create_archive(
    output: Path,
    *,
    passphrase: str,
    sections: dict[str, bytes],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    created_at = utc_now()
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(passphrase, salt)
    with tempfile.TemporaryDirectory(prefix="savedstream-backup-") as temp_dir:
        temp = Path(temp_dir)
        payload_tar = temp / "payload.tar.gz"
        with tarfile.open(payload_tar, "w:gz") as tar:
            for name, content in sections.items():
                safe_name = str(name).replace("\\", "/").lstrip("/")
                if not safe_name or ".." in Path(safe_name).parts:
                    raise SystemBackupError("invalid backup section name")
                item = temp / safe_name
                item.parent.mkdir(parents=True, exist_ok=True)
                item.write_bytes(content)
                tar.add(item, arcname=safe_name, recursive=False)
        encrypted = AESGCM(key).encrypt(nonce, payload_tar.read_bytes(), b"savedstream-system-backup")
        manifest: dict[str, Any] = {
            "format": "savedstream-system-backup",
            "format_version": FORMAT_VERSION,
            "created_at": created_at,
            "filename": output.name,
            "payload_sha256": hashlib.sha256(encrypted).hexdigest(),
            # The archive checksum is defined over the immutable encrypted
            # payload so it can be verified without a self-referential ZIP
            # hash.  The returned manifest also exposes this field.
            "archive_sha256": hashlib.sha256(encrypted).hexdigest(),
            "payload_size": len(encrypted),
            "kdf": {"name": "scrypt", "salt": base64.urlsafe_b64encode(salt).decode("ascii"), "n": 2**14, "r": 8, "p": 1},
            "encryption": {"name": "AES-256-GCM", "nonce": base64.urlsafe_b64encode(nonce).decode("ascii")},
            "sections": sorted(sections),
            "marker": BACKUP_MARKER,
            **(metadata or {}),
        }
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            archive.writestr("payload.bin", encrypted)
    manifest["archive_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    return manifest


def inspect_archive(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            payload = archive.read("payload.bin")
    except Exception as exc:
        raise SystemBackupError("invalid .ssbak archive") from exc
    if manifest.get("format") != "savedstream-system-backup":
        raise SystemBackupError("unsupported backup format")
    if int(manifest.get("format_version", 0)) > FORMAT_VERSION:
        raise SystemBackupError("backup format is newer than this server")
    expected = str(manifest.get("payload_sha256") or "")
    if expected and not hashlib.sha256(payload).hexdigest() == expected:
        raise SystemBackupError("backup payload checksum mismatch")
    return manifest


def extract_archive(path: Path, passphrase: str, destination: Path) -> dict[str, Any]:
    manifest = inspect_archive(path)
    try:
        with zipfile.ZipFile(path) as archive:
            encrypted = archive.read("payload.bin")
        kdf = manifest["kdf"]
        salt = base64.urlsafe_b64decode(kdf["salt"])
        nonce = base64.urlsafe_b64decode(manifest["encryption"]["nonce"])
        plaintext = AESGCM(_derive_key(passphrase, salt)).decrypt(nonce, encrypted, b"savedstream-system-backup")
    except SystemBackupError:
        raise
    except Exception as exc:
        raise SystemBackupError("backup passphrase is incorrect or payload is corrupt") from exc
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination, suffix=".tar.gz", delete=False) as handle:
        handle.write(plaintext)
        tar_path = Path(handle.name)
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            _safe_extract_tar(tar, destination)
    except Exception as exc:
        raise SystemBackupError("backup payload archive is invalid") from exc
    finally:
        tar_path.unlink(missing_ok=True)
    return manifest


def snapshot_sqlite(source: Path, destination: Path, *, excluded_tables: set[str] | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    excluded_tables = excluded_tables or {"auth_sessions", "access_sessions", "trusted_devices"}
    source_db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    target_db = sqlite3.connect(destination)
    try:
        source_db.backup(target_db)
        target_db.commit()
        for table in excluded_tables:
            try:
                target_db.execute(f'DELETE FROM "{table}"')
            except sqlite3.OperationalError:
                pass
        target_db.execute("PRAGMA integrity_check")
        target_db.commit()
    finally:
        source_db.close()
        target_db.close()


def next_cron(expr: str, timezone_name: str, now: datetime | None = None) -> datetime:
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(timezone_name)
    except Exception as exc:
        raise SystemBackupError("invalid backup timezone") from exc
    base = (now or datetime.now(timezone.utc)).astimezone(tz)
    if croniter is None:
        # Minimal fallback for environments where optional dependencies have
        # not been installed yet. Full validation is provided by croniter.
        fields = expr.split()
        if len(fields) != 5:
            raise SystemBackupError("cron expression must have five fields")
        return (base.replace(second=0, microsecond=0) + timedelta(minutes=1)).astimezone(timezone.utc)
    try:
        return croniter(expr, base).get_next(datetime).astimezone(timezone.utc)
    except Exception as exc:
        raise SystemBackupError("invalid cron expression") from exc


def archive_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0

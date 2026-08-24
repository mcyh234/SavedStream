from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_local_env() -> None:
    """Load a developer .env without adding a runtime dependency.

    Docker Compose injects the environment itself. This fallback only helps
    when running uvicorn/python directly from a checkout.
    """
    project_root = Path(__file__).resolve().parents[2]
    env_path = project_root / ".env"
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("\"'")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    admin_key: str
    data_dir: Path
    cookie_secure: bool
    session_cookie_days: int
    telebox_url: str = "http://telebox:9000"
    telebox_api_token: str = ""
    telebox_default_account: str = "default"
    media_cache_key: str = ""
    savedstream_internal_token: str = ""
    backups_dir: Path = Path("/backups")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "savedstream.db"

    @property
    def telegram_session_path(self) -> Path:
        return self.data_dir / "telegram" / "savedstream"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def configuration_ok(self) -> bool:
        # Telegram API IDs are positive integers. Do not treat a negative
        # value as configured just because bool(-1) is true.
        return bool(self.admin_key and self.telebox_url and self.telebox_api_token and self.media_cache_key)

    @classmethod
    def from_env(cls) -> "Settings":
        api_id_raw = os.getenv("TELEGRAM_API_ID", "0").strip()
        try:
            api_id = int(api_id_raw)
        except ValueError:
            api_id = 0

        return cls(
            api_id=api_id,
            api_hash=os.getenv("TELEGRAM_API_HASH", "").strip(),
            admin_key=os.getenv("ADMIN_KEY", "").strip(),
            data_dir=Path(os.getenv("DATA_DIR", "/data")).expanduser().resolve(),
            cookie_secure=_env_bool("COOKIE_SECURE", False),
            session_cookie_days=max(1, int(os.getenv("SESSION_COOKIE_DAYS", "30"))),
            telebox_url=os.getenv("TELEBOX_URL", "http://telebox:9000").rstrip("/"),
            telebox_api_token=os.getenv("TELEBOX_API_TOKEN", "").strip(),
            telebox_default_account=os.getenv("TELEBOX_DEFAULT_ACCOUNT", "default").strip() or "default",
            media_cache_key=os.getenv("MEDIA_CACHE_KEY", "").strip(),
            savedstream_internal_token=os.getenv("SAVEDSTREAM_INTERNAL_TOKEN", "").strip(),
            backups_dir=Path(os.getenv("BACKUPS_DIR", "/backups")).expanduser().resolve(),
        )


_load_local_env()
settings = Settings.from_env()

"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Secrets come only from the environment."""

    session_secret: str = ""
    app_username: str = ""
    app_password_hash: str = ""
    hik_user: str = ""
    hik_password: str = ""
    cookie_secure: bool = True
    forwarded_allow_ips: str = "127.0.0.1"
    data_dir: Path = Path("data")
    max_streams: int = 3
    idle_stop_seconds: float = 30.0
    stall_seconds: float = 2.0
    session_rotate_seconds: float = 225.0
    public_host: str = "0.0.0.0"
    public_port: int = 8080
    internal_port: int = 8081
    go2rtc_url: str = "http://127.0.0.1:1984"
    internal_ts_base: str = "http://127.0.0.1:8081"
    reconcile_seconds: float = 30.0
    web_dir: Path = field(default_factory=lambda: Path("web"))

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        return cls(
            session_secret=env("SESSION_SECRET", ""),
            app_username=env("APP_USERNAME", ""),
            app_password_hash=env("APP_PASSWORD_HASH", ""),
            hik_user=env("HIK_CONNECT_USER", ""),
            hik_password=env("HIK_CONNECT_PASSWORD", ""),
            cookie_secure=_env_bool("COOKIE_SECURE", True),
            forwarded_allow_ips=env("FORWARDED_ALLOW_IPS", "127.0.0.1"),
            data_dir=Path(env("DATA_DIR", "data")),
            max_streams=_env_int("MAX_STREAMS", 3),
            idle_stop_seconds=float(env("IDLE_STOP_SECONDS", "30")),
            stall_seconds=float(env("STALL_SECONDS", "2")),
            session_rotate_seconds=float(env("SESSION_ROTATE_SECONDS", "225")),
            public_port=_env_int("PUBLIC_PORT", 8080),
            internal_port=_env_int("INTERNAL_PORT", 8081),
            go2rtc_url=env("GO2RTC_URL", "http://127.0.0.1:1984"),
            internal_ts_base=env("INTERNAL_TS_BASE", "http://127.0.0.1:8081"),
            web_dir=Path(env("WEB_DIR", "web")),
        )

    def validate(self) -> None:
        """Refuse to start without the app login and the cookie secret."""
        required = {
            "SESSION_SECRET": self.session_secret,
            "APP_USERNAME": self.app_username,
            "APP_PASSWORD_HASH": self.app_password_hash,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise SystemExit(f"missing required env: {', '.join(missing)}")

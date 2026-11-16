"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Secrets come only from the environment."""

    secret_key: str = ""
    session_secret: str = ""
    data_dir: Path = Path("data")
    max_streams: int = 3
    idle_stop_seconds: float = 30.0
    stall_seconds: float = 2.0
    session_rotate_seconds: float = 225.0
    public_host: str = "0.0.0.0"
    public_port: int = 8080
    internal_port: int = 8081
    go2rtc_url: str = "http://127.0.0.1:1984"
    go2rtc_rtsp_url: str = "rtsp://127.0.0.1:8554"
    internal_ts_base: str = "http://127.0.0.1:8081"
    reconcile_seconds: float = 30.0
    web_dir: Path = field(default_factory=lambda: Path("web"))

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            secret_key=os.environ.get("CAMVIEWER_SECRET_KEY", ""),
            session_secret=os.environ.get("SESSION_SECRET", ""),
            data_dir=Path(os.environ.get("DATA_DIR", "data")),
            max_streams=_env_int("MAX_STREAMS", 3),
            idle_stop_seconds=float(os.environ.get("IDLE_STOP_SECONDS", "30")),
            public_port=_env_int("PUBLIC_PORT", 8080),
            internal_port=_env_int("INTERNAL_PORT", 8081),
            go2rtc_url=os.environ.get("GO2RTC_URL", "http://127.0.0.1:1984"),
            go2rtc_rtsp_url=os.environ.get("GO2RTC_RTSP_URL", "rtsp://127.0.0.1:8554"),
            internal_ts_base=os.environ.get("INTERNAL_TS_BASE", "http://127.0.0.1:8081"),
            stall_seconds=float(os.environ.get("STALL_SECONDS", "2")),
            session_rotate_seconds=float(os.environ.get("SESSION_ROTATE_SECONDS", "225")),
            web_dir=Path(os.environ.get("WEB_DIR", "web")),
        )

    def validate(self) -> None:
        """Refuse to start without both secrets."""
        missing = [
            name
            for name, value in (
                ("CAMVIEWER_SECRET_KEY", self.secret_key),
                ("SESSION_SECRET", self.session_secret),
            )
            if not value
        ]
        if missing:
            raise SystemExit(f"missing required env: {', '.join(missing)}")

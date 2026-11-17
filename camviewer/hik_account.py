"""Single shared Hik-Connect cloud account: one login, camera cache, stream opening."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from hikcloudstream import (
    AuthenticationError,
    CaptchaRequiredError,
    Credentials,
    HikConnectClient,
    StreamType,
)
from hikcloudstream.exceptions import StreamNegotiationError
from hikcloudstream.stream import open_live_stream
from pyezvizapi.exceptions import DeviceException

from camviewer.account_state import AccountState

log = logging.getLogger("camviewer.account")

CAMERA_TTL_SECONDS = 300
RELOGIN_MIN_INTERVAL = 30.0
OPEN_TIMEOUT_SECONDS = 5.0
ONLINE_TTL_SECONDS = 20.0


class AuthError(Exception):
    """Cloud rejected the credentials."""


class CaptchaError(AuthError):
    """Cloud demands CAPTCHA; the user must log in via the official app."""


class StreamError(Exception):
    """Live stream could not be opened (offline camera, negotiation failure)."""


def classify(exc: BaseException) -> str:
    """Map a stream-loop exception to auth | captcha | transient | offline | other."""
    if isinstance(exc, CaptchaError | CaptchaRequiredError):
        return "captcha"
    if isinstance(exc, AuthError | AuthenticationError):
        return "auth"
    if isinstance(exc, StreamNegotiationError) and "VTM resource" in str(exc):
        return "transient"  # right after a session ends the cloud briefly forgets the resource
    if isinstance(exc, DeviceException | StreamError):
        return "offline"
    return "other"


class HikAccount:
    """Thread-safe wrapper; the lib client and stream opener are injectable for tests."""

    def __init__(
        self,
        state: AccountState,
        client_factory: Callable[[], Any] = HikConnectClient,
        opener: Callable[..., Any] = open_live_stream,
    ) -> None:
        self.state = state
        self._factory = client_factory
        self._opener = opener
        self._lock = threading.RLock()
        self._client: Any | None = None
        self._creds: Credentials | None = None
        self._cameras: list[Any] = []
        self._cameras_at = 0.0
        self._last_relogin = 0.0
        self._online: dict[str, bool] = {}
        self._online_at = -ONLINE_TTL_SECONDS

    @property
    def username(self) -> str | None:
        return self._creds.username if self._creds else None

    def login(self, username: str, password: str) -> None:
        """Real cloud login. Raises AuthError / CaptchaError; updates account state."""
        creds = Credentials(username, password)
        with self._lock:
            client = self._factory()
            try:
                client.login(creds)
            except CaptchaRequiredError as exc:
                self.state.mark_needs_attention("captcha")
                raise CaptchaError(str(exc)) from exc
            except AuthenticationError as exc:
                raise AuthError(str(exc)) from exc
            self._swap_client(client)
            self._creds = creds
            self._cameras_at = 0.0
            self.state.mark_ok()

    def _swap_client(self, client: Any) -> None:
        old, self._client = self._client, client
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001 - best effort close of a stale client
                log.debug("stale client close failed")

    def relogin(self) -> None:
        """Re-authenticate with in-memory creds. Counts failures toward the breaker."""
        with self._lock:
            if self._creds is None:
                raise AuthError("no credentials")
            if not self.state.usable:
                raise AuthError("account needs login")
            wait = RELOGIN_MIN_INTERVAL - (time.monotonic() - self._last_relogin)
            if wait > 0 and self._last_relogin:
                raise AuthError("relogin rate limited")
            self._last_relogin = time.monotonic()
            client = self._factory()
            try:
                client.login(self._creds)
            except CaptchaRequiredError as exc:
                self.state.mark_needs_attention("captcha")
                raise CaptchaError(str(exc)) from exc
            except AuthenticationError as exc:
                if self.state.record_auth_failure():
                    log.warning("account needs attention after repeated auth failures")
                raise AuthError(str(exc)) from exc
            self._swap_client(client)
            self.state.mark_ok()

    def online_map(self) -> dict[str, bool]:
        """Device serial -> online, from the account's device list (cached briefly)."""
        with self._lock:
            if self._client is None:
                return {}
            if time.monotonic() - self._online_at < ONLINE_TTL_SECONDS:
                return dict(self._online)
            try:
                payload = self._device_status_page()
            except Exception:  # noqa: BLE001 - usually an expired cloud token
                # Nothing else re-logs in while no one is streaming, so an expired token would
                # keep serving the last answer forever (a camera stuck "offline"). Re-login once;
                # if that fails too, report "unknown" (treated as online) rather than stale data.
                try:
                    self.relogin()
                    payload = self._device_status_page()
                except Exception as exc:  # noqa: BLE001 - online flag is advisory only
                    log.warning("device status lookup failed: %s", type(exc).__name__)
                    self._online = {}
                    self._online_at = time.monotonic()
                    return {}
            self._online = {
                str(d["deviceSerial"]): d.get("status") == 1
                for d in payload.get("deviceInfos") or []
                if d.get("deviceSerial")
            }
            self._online_at = time.monotonic()
            return dict(self._online)

    def _device_status_page(self) -> dict[str, Any]:
        assert self._client is not None
        return self._client.request_json(
            "GET",
            "/v3/userdevices/v1/devices/pagelist",
            params={"groupId": -1, "limit": 50, "offset": 0, "filter": "STATUS"},
        )

    def cameras(self, *, force: bool = False) -> list[Any]:
        with self._lock:
            if self._client is None:
                return []
            fresh = time.monotonic() - self._cameras_at < CAMERA_TTL_SECONDS
            if force or not self._cameras or not fresh:
                self._cameras = self._client.list_cameras()
                self._cameras_at = time.monotonic()
            return list(self._cameras)

    def snapshot(self, camera: Any) -> bytes:
        with self._lock:
            client = self._client
        if client is None:
            raise AuthError("not logged in")
        return client.capture_snapshot(camera)

    def open_stream(self, camera: Any) -> Any:
        """Open a MAIN VTM session (SUB carries no data on these cameras)."""
        with self._lock:
            client = self._client
        if client is None:
            raise AuthError("not logged in")
        return self._opener(
            client, camera, stream_type=StreamType.MAIN, timeout=OPEN_TIMEOUT_SECONDS
        )

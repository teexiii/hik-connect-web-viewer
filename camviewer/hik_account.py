"""Single shared Hik-Connect cloud account: one login, camera cache, stream opening."""

from __future__ import annotations

import base64
import json
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
# The cloud session token is a JWT that lives 24 h. Past its expiry the cloud still answers the
# device list, with every device reported offline, so nothing fails and the cameras just vanish.
SESSION_REFRESH_MARGIN = 3600.0  # re-login this long before the token expires
SESSION_MIN_LIFETIME = 600.0  # never schedule a refresh sooner than this after a login
SESSION_FALLBACK_LIFETIME = 12 * 3600.0  # when the token carries no readable expiry
SESSION_REFRESH_RETRY = 300.0  # pace of retries while the scheduled re-login keeps failing
# "No camera online" is double-checked on a fresh session at most this often: a session can also
# go stale before its expiry (for example when the cloud revokes it).
OFFLINE_RECHECK_SECONDS = 600.0


class AuthError(Exception):
    """Cloud rejected the credentials."""


class CaptchaError(AuthError):
    """Cloud demands CAPTCHA; the user must log in via the official app."""


class StreamError(Exception):
    """Live stream could not be opened (offline camera, negotiation failure)."""


def session_lifetime(session_id: Any) -> float:
    """Seconds until the session token (a JWT with an `exp` claim) expires."""
    try:
        part = session_id.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return float(claims["exp"]) - time.time()
    except Exception:  # noqa: BLE001 - not a JWT, or no expiry: assume a conservative lifetime
        return SESSION_FALLBACK_LIFETIME


def _status_by_serial(payload: dict[str, Any]) -> dict[str, bool]:
    return {
        str(d["deviceSerial"]): d.get("status") == 1
        for d in payload.get("deviceInfos") or []
        if d.get("deviceSerial")
    }


def classify(exc: BaseException) -> str:
    """Map a stream-loop exception to auth | captcha | transient | busy | offline | other."""
    if isinstance(exc, CaptchaError | CaptchaRequiredError):
        return "captcha"
    if isinstance(exc, AuthError | AuthenticationError):
        return "auth"
    if isinstance(exc, StreamNegotiationError) and "VTM resource" in str(exc):
        return "transient"  # right after a session ends the cloud briefly forgets the resource
    if isinstance(exc, StreamError) and "5404" in str(exc):
        # The camera refuses a new session for a minute or two after the previous one ended
        # (rotation, a viewer leaving, a redeploy). It is online; keep retrying at a steady pace.
        return "busy"
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
        self._refresh_at = 0.0
        self._offline_rechecked_at = -OFFLINE_RECHECK_SECONDS

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
        lifetime = session_lifetime(getattr(client, "session_id", None))
        self._refresh_at = time.monotonic() + max(
            lifetime - SESSION_REFRESH_MARGIN, SESSION_MIN_LIFETIME
        )
        self._online_at = -ONLINE_TTL_SECONDS  # status read on the old session may be stale
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

    def _refresh_session_if_due(self) -> None:
        """Re-login shortly before the session token expires. Best effort: a failure keeps the
        current session and is retried later (auth failures still count toward the breaker)."""
        with self._lock:
            if self._client is None or time.monotonic() < self._refresh_at:
                return
            try:
                self.relogin()
                log.info("cloud session refreshed before expiry")
            except Exception as exc:  # noqa: BLE001 - cloud unreachable, rate limit, breaker
                self._refresh_at = time.monotonic() + SESSION_REFRESH_RETRY
                log.warning("scheduled session refresh failed: %s", type(exc).__name__)

    def online_map(self) -> dict[str, bool]:
        """Device serial -> online, from the account's device list (cached briefly)."""
        with self._lock:
            if self._client is None:
                return {}
            self._refresh_session_if_due()
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
            online = _status_by_serial(payload)
            if online and not any(online.values()):
                online = self._recheck_all_offline(online)
            self._online = online
            self._online_at = time.monotonic()
            return dict(self._online)

    def _recheck_all_offline(self, online: dict[str, bool]) -> dict[str, bool]:
        """A stale session reports every device offline. Before believing that, ask again on a
        fresh session (rate limited); if that is not possible keep the first answer."""
        if time.monotonic() - self._offline_rechecked_at < OFFLINE_RECHECK_SECONDS:
            return online
        self._offline_rechecked_at = time.monotonic()
        try:
            self.relogin()
            fresh = _status_by_serial(self._device_status_page())
        except Exception as exc:  # noqa: BLE001 - keep the first answer
            log.warning("offline recheck failed: %s", type(exc).__name__)
            return online
        if any(fresh.values()):
            log.warning("stale session reported every camera offline; fresh session disagrees")
        return fresh

    def _device_status_page(self) -> dict[str, Any]:
        assert self._client is not None
        payload = self._client.request_json(
            "GET",
            "/v3/userdevices/v1/devices/pagelist",
            params={"groupId": -1, "limit": 50, "offset": 0, "filter": "STATUS"},
        )
        # An expired token is answered without raising, just with no device list; treat that as a
        # failure so the caller re-logs in instead of reporting every camera as "unknown" forever.
        if not isinstance(payload, dict) or "deviceInfos" not in payload:
            code = (payload.get("meta") or {}).get("code") if isinstance(payload, dict) else None
            raise RuntimeError(f"device status answer without deviceInfos (meta code {code})")
        return payload

    def cameras(self, *, force: bool = False) -> list[Any]:
        with self._lock:
            if self._client is None:
                return []
            self._refresh_session_if_due()
            fresh = time.monotonic() - self._cameras_at < CAMERA_TTL_SECONDS
            if force or not self._cameras or not fresh:
                try:
                    self._cameras = self._client.list_cameras()
                except Exception:  # noqa: BLE001 - usually an expired cloud token
                    self.relogin()
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
        self._refresh_session_if_due()  # a viewer watching for hours never lists cameras again
        with self._lock:
            client = self._client
        if client is None:
            raise AuthError("not logged in")
        return self._opener(
            client, camera, stream_type=StreamType.MAIN, timeout=OPEN_TIMEOUT_SECONDS
        )

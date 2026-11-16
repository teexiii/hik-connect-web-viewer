"""One camera ingest: VTM session loop feeding a single long-lived ffmpeg -> MPEG-TS.

The ffmpeg process outlives VTM sessions, so a cloud-side session timeout only shows
up downstream as a short pause in an otherwise continuous MPEG-TS stream.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
from typing import Any

from pyezvizapi.stream import StreamTransport, detect_transport

from camviewer.hik_account import AuthError, HikAccount, StreamError, classify

log = logging.getLogger("camviewer.ingest")

GENERIC_BACKOFF = (0.5, 1.0, 2.0, 5.0)
OFFLINE_BACKOFF = (2.0, 5.0, 10.0, 20.0, 40.0, 60.0)
TRANSIENT_BACKOFF = (1.0, 2.0, 5.0)
FIRST_DATA_SECONDS = 6.0
STABLE_SECONDS = 60.0
AUTH_STREAK_LIMIT = 3
QUEUE_SIZE = 128
TS_READ_SIZE = 188 * 64

# Input demuxer per detected payload transport.
INPUT_FORMAT = {
    StreamTransport.MPEG_PS: "mpeg",
    StreamTransport.MPEG_TS: "mpegts",
}


def backoff_delay(attempt: int, *, offline: bool = False) -> float:
    """Delay before reconnect attempt number `attempt` (0-based)."""
    table = OFFLINE_BACKOFF if offline else GENERIC_BACKOFF
    return table[min(attempt, len(table) - 1)]


def ffmpeg_command(ffmpeg_path: str, input_format: str) -> list[str]:
    return [
        ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-probesize", "262144", "-analyzeduration", "1000000",
        "-fflags", "+genpts+igndts+discardcorrupt",
        "-use_wallclock_as_timestamps", "1",
        "-f", input_format, "-i", "pipe:0",
        "-c", "copy", "-f", "mpegts", "-muxdelay", "0", "pipe:1",
    ]  # fmt: skip


class StreamIngest:
    """Runs in worker threads; hands MPEG-TS bytes to asyncio subscriber queues."""

    def __init__(
        self,
        stream_id: str,
        camera: Any,
        account: HikAccount,
        loop: asyncio.AbstractEventLoop,
        *,
        stall_seconds: float = 2.0,
        rotate_seconds: float = 225.0,
        ffmpeg_path: str = "ffmpeg",
    ) -> None:
        self.stream_id = stream_id
        self.camera = camera
        self.account = account
        self.loop = loop
        self.stall_seconds = stall_seconds
        self.rotate_seconds = rotate_seconds
        self.ffmpeg_path = ffmpeg_path
        self.subscribers: set[asyncio.Queue[bytes | None]] = set()
        self.state = "starting"
        self.reconnects = 0
        self.rotations = 0
        self._session_started = 0.0
        self._got_data = False
        self._transient_streak = 0
        self.last_bytes_at = 0.0
        self._stop = threading.Event()
        self._forced = threading.Event()
        self._session: Any | None = None
        self._ff: subprocess.Popen[bytes] | None = None
        self._ff_format: str | None = None
        self._auth_streak = 0
        self._threads: list[threading.Thread] = []

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        for target, name in ((self._run, "ingest"), (self._watchdog, "watchdog")):
            thread = threading.Thread(target=target, name=f"{name}-{self.stream_id}", daemon=True)
            thread.start()
            self._threads.append(thread)

    @property
    def alive(self) -> bool:
        return bool(self._threads) and self._threads[0].is_alive()

    def stop(self) -> None:
        self._stop.set()
        self._close_session()
        self._stop_ffmpeg()
        for thread in self._threads:
            thread.join(timeout=5)
        self._stop_ffmpeg()  # a write racing with stop may have respawned it
        for queue in list(self.subscribers):
            self._offer(queue, None)

    def force_reconnect(self) -> None:
        """Drop the current VTM session; the loop reopens it with the same ffmpeg."""
        self._forced.set()
        self._close_session()

    def health(self) -> dict[str, Any]:
        age = time.monotonic() - self.last_bytes_at if self.last_bytes_at else None
        return {
            "state": self.state,
            "reconnects": self.reconnects,
            "rotations": self.rotations,
            "last_bytes_age": None if age is None else round(age, 1),
            "subscribers": len(self.subscribers),
        }

    # ---- session loop ----------------------------------------------------
    def _close_session(self) -> None:
        session = self._session
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - closing only needs to unblock the reader
                log.debug("session close failed stream=%s", self.stream_id)

    def _run(self) -> None:
        attempt = 0
        first = True
        while not self._stop.is_set():
            self.state = "starting" if first else "reconnecting"
            first = False
            started = time.monotonic()
            failure: BaseException | None = None
            try:
                self._run_session()
            except Exception as exc:  # noqa: BLE001 - classified below
                failure = exc
            if self._stop.is_set():
                break
            forced = self._forced.is_set()
            self._forced.clear()
            kind = "forced" if forced else classify(failure) if failure else "ended"
            if kind == "transient":
                self._transient_streak += 1
                if self._transient_streak > len(TRANSIENT_BACKOFF):
                    kind = "offline"
            if not self._after_failure(kind, failure):
                break
            self.reconnects += 1
            if time.monotonic() - started > STABLE_SECONDS:
                attempt = 0
            if forced:
                delay = 0.0
            elif kind == "transient":
                delay = TRANSIENT_BACKOFF[self._transient_streak - 1]
            else:
                delay = backoff_delay(attempt, offline=kind == "offline")
            attempt = 0 if forced else attempt + 1
            log.info("stream=%s reconnect kind=%s delay=%.1fs", self.stream_id, kind, delay)
            self._stop.wait(delay)
        if not self._stop.is_set():
            try:
                self.loop.call_soon_threadsafe(self._end_subscribers)
            except RuntimeError:  # event loop closed during shutdown
                pass

    def _after_failure(self, kind: str, failure: BaseException | None) -> bool:
        """Update state for the failure kind; False means stop the ingest."""
        if kind == "captcha":
            self.state = "failed"
            return False
        if kind == "auth":
            self._auth_streak += 1
            if self._auth_streak >= AUTH_STREAK_LIMIT:
                self.account.state.mark_needs_login()
            else:
                try:
                    self.account.relogin()
                except AuthError:
                    log.warning("relogin failed stream=%s", self.stream_id)
        if not self.account.state.usable:
            self.state = "failed"
            return False
        self.state = "offline" if kind == "offline" else "reconnecting"
        if failure is not None and kind in ("offline", "other"):
            log.warning("stream=%s %s: %s", self.stream_id, type(failure).__name__, failure)
        return True

    def _run_session(self) -> None:
        session = self.account.open_stream(self.camera)
        self._session = session
        self._session_started = self.last_bytes_at = time.monotonic()
        self._got_data = False
        try:
            info = session.start()
            result = getattr(info, "result", 0)
            if result not in (0, None):
                raise StreamError(f"stream start result {result}")
            self.last_bytes_at = time.monotonic()
            fmt: str | None = None
            for chunk in session.iter_annex_b_chunks():
                if self._stop.is_set():
                    return
                if fmt is None:
                    fmt = INPUT_FORMAT.get(detect_transport(chunk), "h264")
                self._write(chunk, fmt)
                self.last_bytes_at = time.monotonic()
                self._auth_streak = 0
                self._transient_streak = 0
                self._got_data = True
                self.state = "live"
        finally:
            self._session = None
            try:
                session.close()
            except Exception:  # noqa: BLE001
                log.debug("session close failed stream=%s", self.stream_id)

    def _watchdog(self) -> None:
        """Close the session on a stall (unblocks the reader) or when it is due for rotation.

        The cloud silently stops sending data ~240s into a session and the library only
        notices after its own 15s timeout, so we rotate early and detect stalls fast.
        """
        tick = max(0.1, min(1.0, self.stall_seconds / 4))
        while not self._stop.wait(tick):
            if self._session is None:
                continue
            now = time.monotonic()
            limit = self.stall_seconds
            if not self._got_data:
                limit = max(limit, FIRST_DATA_SECONDS)
            if now - self.last_bytes_at > limit:
                log.warning("stream=%s stalled, forcing reconnect", self.stream_id)
                self.last_bytes_at = now
                self.force_reconnect()
            elif self._got_data and now - self._session_started > self.rotate_seconds:
                log.info("stream=%s rotating session", self.stream_id)
                self.rotations += 1
                self._session_started = now
                self.force_reconnect()

    # ---- ffmpeg ----------------------------------------------------------
    def _write(self, chunk: bytes, fmt: str) -> None:
        for _ in range(2):
            if self._ff is None or self._ff.poll() is not None or self._ff_format != fmt:
                self._start_ffmpeg(fmt)
            try:
                assert self._ff is not None and self._ff.stdin is not None
                self._ff.stdin.write(chunk)
                self._ff.stdin.flush()
                return
            except (OSError, ValueError):
                self._stop_ffmpeg()

    def _start_ffmpeg(self, fmt: str) -> None:
        self._stop_ffmpeg()
        proc = subprocess.Popen(
            ffmpeg_command(self.ffmpeg_path, fmt),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._ff, self._ff_format = proc, fmt
        for target in (self._read_ts, self._drain_stderr):
            threading.Thread(target=target, args=(proc,), daemon=True).start()

    def _stop_ffmpeg(self) -> None:
        proc, self._ff, self._ff_format = self._ff, None, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def _read_ts(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        while data := proc.stdout.read(TS_READ_SIZE):
            try:
                self.loop.call_soon_threadsafe(self._broadcast, data)
            except RuntimeError:  # event loop already closed during shutdown
                break
        proc.stdout.close()

    def _drain_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            text = line.decode(errors="replace").strip()
            log.warning("ffmpeg stream=%s: %s", self.stream_id, text)
        proc.stderr.close()

    # ---- fan-out (event loop thread) ---------------------------------------
    def _end_subscribers(self) -> None:
        """Tell consumers the ingest is over so their HTTP source closes."""
        for queue in list(self.subscribers):
            self._offer(queue, None)

    def _broadcast(self, data: bytes) -> None:
        for queue in self.subscribers:
            self._offer(queue, data)

    @staticmethod
    def _offer(queue: asyncio.Queue[bytes | None], item: bytes | None) -> None:
        """Bounded queue; a slow consumer loses the oldest data, never blocks ingest."""
        while True:
            try:
                queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

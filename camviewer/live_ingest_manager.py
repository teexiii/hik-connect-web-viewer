"""Owns the per-camera ingests: on-demand start, idle stop, stream cap, health."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from camviewer.hik_account import HikAccount
from camviewer.stream_ingest import QUEUE_SIZE, StreamIngest

log = logging.getLogger("camviewer.manager")


class TooManyStreams(Exception):
    """MAX_STREAMS concurrent ingests already running."""


class UnknownStream(Exception):
    """Stream id does not match any camera on the account."""


def stream_id_for(camera: Any) -> str:
    return f"{camera.device_serial}_{camera.channel_no}"


class LiveIngestManager:
    def __init__(
        self,
        account: HikAccount,
        *,
        max_streams: int = 3,
        idle_stop_seconds: float = 30.0,
        stall_seconds: float = 2.0,
        rotate_seconds: float = 150.0,
        ffmpeg_path: str = "ffmpeg",
    ) -> None:
        self.account = account
        self.max_streams = max_streams
        self.idle_stop_seconds = idle_stop_seconds
        self.stall_seconds = stall_seconds
        self.rotate_seconds = rotate_seconds
        self.ffmpeg_path = ffmpeg_path
        self._ensure_lock = asyncio.Lock()
        self._ingests: dict[str, StreamIngest] = {}
        self._idle_handles: dict[str, asyncio.TimerHandle] = {}

    async def find_camera(self, stream_id: str) -> Any:
        cameras = await asyncio.to_thread(self.account.cameras)
        for camera in cameras:
            if stream_id_for(camera) == stream_id:
                return camera
        raise UnknownStream(stream_id)

    async def ensure(self, stream_id: str) -> StreamIngest:
        # One caller at a time: replacing a dead ingest awaits, and a concurrent caller must not
        # create a second ingest that would be orphaned (an extra cloud session + ffmpeg).
        async with self._ensure_lock:
            return await self._ensure_locked(stream_id)

    async def _ensure_locked(self, stream_id: str) -> StreamIngest:
        ingest = self._ingests.get(stream_id)
        if ingest is not None and ingest.alive:
            return ingest
        camera = await self.find_camera(stream_id)
        ingest = self._ingests.get(stream_id)  # another subscriber may have won the race
        if ingest is not None and ingest.alive:
            return ingest
        if ingest is not None:  # a failed ingest (account needed login) is replaced
            del self._ingests[stream_id]
            await asyncio.to_thread(ingest.stop)
        if len(self._ingests) >= self.max_streams:
            raise TooManyStreams(stream_id)
        ingest = StreamIngest(
            stream_id,
            camera,
            self.account,
            asyncio.get_running_loop(),
            stall_seconds=self.stall_seconds,
            rotate_seconds=self.rotate_seconds,
            ffmpeg_path=self.ffmpeg_path,
        )
        self._ingests[stream_id] = ingest
        ingest.start()
        return ingest

    async def subscribe(self, stream_id: str) -> AsyncIterator[bytes]:
        """Yield MPEG-TS bytes for a camera; starts the ingest if needed."""
        ingest = await self.ensure(stream_id)
        self._cancel_idle(stream_id)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=QUEUE_SIZE)
        ingest.subscribers.add(queue)
        try:
            while (item := await queue.get()) is not None:
                yield item
        finally:
            ingest.subscribers.discard(queue)
            if not ingest.subscribers:
                self._schedule_idle(stream_id, ingest)

    def _cancel_idle(self, stream_id: str) -> None:
        handle = self._idle_handles.pop(stream_id, None)
        if handle:
            handle.cancel()

    def _schedule_idle(self, stream_id: str, ingest: StreamIngest) -> None:
        self._cancel_idle(stream_id)
        loop = asyncio.get_running_loop()
        self._idle_handles[stream_id] = loop.call_later(
            self.idle_stop_seconds, self._idle_stop, stream_id, ingest
        )

    def _idle_stop(self, stream_id: str, ingest: StreamIngest) -> None:
        self._idle_handles.pop(stream_id, None)
        if ingest.subscribers or self._ingests.get(stream_id) is not ingest:
            return
        del self._ingests[stream_id]
        log.info("stream=%s idle, stopping", stream_id)
        asyncio.get_running_loop().run_in_executor(None, ingest.stop)

    def force_reconnect(self, stream_id: str) -> bool:
        ingest = self._ingests.get(stream_id)
        if ingest is None:
            return False
        ingest.force_reconnect()
        return True

    def status(self) -> dict[str, dict[str, Any]]:
        return {sid: ingest.health() for sid, ingest in self._ingests.items()}

    def stop_all(self) -> None:
        for handle in self._idle_handles.values():
            handle.cancel()
        self._idle_handles.clear()
        ingests, self._ingests = list(self._ingests.values()), {}
        for ingest in ingests:
            with contextlib.suppress(Exception):
                ingest.stop()

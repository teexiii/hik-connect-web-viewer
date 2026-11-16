"""Registers camera streams in go2rtc and keeps them registered across go2rtc restarts."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("camviewer.go2rtc")


def stream_names(stream_id: str) -> tuple[str, str]:
    """(HEVC passthrough name, H.264 transcode name) for one camera."""
    return stream_id, f"{stream_id}_h264"


class Go2RtcClient:
    def __init__(self, api_url: str, internal_ts_base: str, http: httpx.AsyncClient) -> None:
        self._api = api_url.rstrip("/")
        self._ts_base = internal_ts_base.rstrip("/")
        self._http = http

    def source_for(self, stream_id: str, name: str) -> str:
        """Both streams read our MPEG-TS directly; going through go2rtc's own RTSP would route
        HEVC through its RTP payloader, which panics on this camera's stream."""
        ts_url = f"{self._ts_base}/internal/ts/{stream_id}"
        return ts_url if name == stream_id else f"ffmpeg:{ts_url}#video=h264"

    async def existing(self) -> set[str]:
        response = await self._http.get(f"{self._api}/api/streams")
        response.raise_for_status()
        return set(response.json())

    async def reconcile(self, stream_ids: list[str]) -> int:
        """PUT every missing stream; returns how many were (re)registered."""
        try:
            present = await self.existing()
        except httpx.HTTPError:
            log.debug("go2rtc not reachable yet")
            return 0
        added = 0
        for stream_id in stream_ids:
            for name in stream_names(stream_id):
                if name in present:
                    continue
                response = await self._http.put(
                    f"{self._api}/api/streams",
                    params={"name": name, "src": self.source_for(stream_id, name)},
                )
                if response.is_success:
                    added += 1
                else:
                    log.warning("register %s failed: %s", name, response.status_code)
        return added

"""H.264 transcode of a camera's MPEG-TS, run with the app image's ffmpeg.

go2rtc's bundled ffmpeg (8.x) mis-decodes this camera's HEVC ("Error constructing the frame
RPS") and smears the picture, while the Debian ffmpeg in the app image decodes it cleanly.
So the transcode happens here and go2rtc only relays the finished H.264 TS.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

log = logging.getLogger("camviewer.h264")

READ_SIZE = 64 * 1024


def transcode_command(ffmpeg_path: str) -> list[str]:
    return [
        ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+discardcorrupt", "-f", "mpegts", "-i", "pipe:0",
        "-map", "0:v:0", "-an",
        # The cloud switches resolution mid-stream; a fixed output size keeps browser decoders
        # (iOS Safari especially) from painting green borders or failing on the size change.
        "-vf", "scale=1280:720,setsar=1",
        "-c:v", "libx264", "-preset", "superfast", "-tune", "zerolatency",
        "-profile:v", "high", "-level:v", "4.1", "-pix_fmt", "yuv420p", "-g", "30",
        "-f", "mpegts", "-muxdelay", "0", "pipe:1",
    ]  # fmt: skip


async def transcode(source: AsyncIterator[bytes], ffmpeg_path: str) -> AsyncIterator[bytes]:
    """Feeds `source` (HEVC MPEG-TS chunks) into ffmpeg and yields H.264 MPEG-TS chunks."""
    proc = await asyncio.create_subprocess_exec(
        *transcode_command(ffmpeg_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout and proc.stderr

    async def feed() -> None:
        try:
            async for chunk in source:
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()

    async def drain_errors() -> None:
        async for line in proc.stderr:
            log.warning("h264 ffmpeg: %s", line.decode(errors="replace").rstrip())

    feeder = asyncio.create_task(feed())
    errors = asyncio.create_task(drain_errors())
    try:
        while chunk := await proc.stdout.read(READ_SIZE):
            yield chunk
    finally:
        feeder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feeder
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        errors.cancel()
        with contextlib.suppress(Exception):
            await source.aclose()  # type: ignore[attr-defined]

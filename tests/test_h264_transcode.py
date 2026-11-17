"""The transcode pipe: source chunks go into the ffmpeg process, its stdout comes back out."""

import sys

import pytest

from camviewer import h264_transcode

ECHO = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"]


@pytest.mark.asyncio
async def test_transcode_pipes_source_through_the_process(monkeypatch):
    monkeypatch.setattr(h264_transcode, "transcode_command", lambda _path: ECHO)

    async def source():
        for chunk in (b"abc", b"def"):
            yield chunk

    out = b"".join([chunk async for chunk in h264_transcode.transcode(source(), "ffmpeg")])
    assert out == b"abcdef"


def test_transcode_command_is_video_only_h264():
    cmd = h264_transcode.transcode_command("ffmpeg")
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-vf") + 1].startswith("scale=1280:720")
    assert "-an" in cmd

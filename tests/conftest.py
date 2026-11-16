"""Shared fixtures: a real MPEG-PS clip and a fake Hik account at the library boundary."""

from __future__ import annotations

import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

from camviewer.account_state import AccountState

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


@pytest.fixture(scope="session")
def ps_chunks() -> list[bytes]:
    """Two seconds of real MPEG-PS video split into network-sized chunks."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
         "-t", "2", "-c:v", "mpeg2video", "-f", "mpeg", "pipe:1"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return [out[i : i + 4096] for i in range(0, len(out), 4096)]


class FakeSession:
    """Mimics LiveStreamSession: start(), blocking iterator, close() unblocks it."""

    def __init__(self, chunks, *, result=0, raise_on_start=None, block_after=False, tail=None):
        self.chunks = chunks
        self.result = result
        self.raise_on_start = raise_on_start
        self.block_after = block_after
        self.tail = tail
        self.closed = threading.Event()

    def start(self):
        if self.raise_on_start:
            raise self.raise_on_start
        return SimpleNamespace(result=self.result)

    def iter_annex_b_chunks(self):
        for chunk in self.chunks:
            if self.closed.is_set():
                return
            yield chunk
        if self.block_after:
            self.closed.wait(timeout=30)
        if self.tail:
            raise self.tail

    def close(self):
        self.closed.set()


class FakeAccount:
    """HikAccount stand-in; `plan` is a list of FakeSession factories, last one repeats."""

    def __init__(self, tmp_path, plan):
        self.state = AccountState(tmp_path)
        self.state.mark_ok()
        self.plan = plan
        self.opened = 0
        self.relogins = 0
        self.relogin_error = None
        self.camera = SimpleNamespace(device_serial="SER1", channel_no=1, name="Cam")

    def cameras(self, force=False):
        return [self.camera]

    def open_stream(self, camera):
        factory = self.plan[min(self.opened, len(self.plan) - 1)]
        self.opened += 1
        return factory()

    def relogin(self):
        self.relogins += 1
        if self.relogin_error:
            raise self.relogin_error

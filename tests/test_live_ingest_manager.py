from __future__ import annotations

import asyncio
import json
import subprocess

import pytest
from hikcloudstream import AuthenticationError, CaptchaRequiredError

from camviewer import stream_ingest
from camviewer.live_ingest_manager import LiveIngestManager, TooManyStreams, UnknownStream
from tests.conftest import FakeAccount, FakeSession

SID = "SER1_1"


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(stream_ingest, "GENERIC_BACKOFF", (0.05,))
    monkeypatch.setattr(stream_ingest, "OFFLINE_BACKOFF", (0.05,))


async def collect(manager, *, min_bytes, timeout=20.0):
    """Subscribe and gather bytes until `min_bytes` or timeout."""
    got = bytearray()

    async def run():
        async for data in manager.subscribe(SID):
            got.extend(data)
            if len(got) >= min_bytes:
                return

    await asyncio.wait_for(run(), timeout)
    return bytes(got)


async def test_session_end_reopens_and_output_stays_one_stream(tmp_path, ps_chunks):
    account = FakeAccount(tmp_path, [lambda: FakeSession(ps_chunks)])
    manager = LiveIngestManager(account, idle_stop_seconds=5)
    # Three sessions' worth of data must flow through the same ffmpeg.
    total = sum(len(c) for c in ps_chunks) * 3
    try:
        data = await collect(manager, min_bytes=total // 2)
        ingest = manager._ingests[SID]
        proc = ingest._ff
        while ingest.reconnects < 2:
            await asyncio.sleep(0.1)
        assert ingest._ff is proc, "ffmpeg must survive VTM reconnects"
        assert account.opened >= 2
    finally:
        manager.stop_all()
    ts = tmp_path / "out.ts"
    ts.write_bytes(data)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(ts)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    streams = json.loads(probe.stdout)["streams"]
    assert [s["codec_type"] for s in streams] == ["video"]


async def test_auth_error_relogins_once_then_resumes(tmp_path, ps_chunks):
    plan = [
        lambda: FakeSession([], raise_on_start=AuthenticationError("expired")),
        lambda: FakeSession(ps_chunks),
    ]
    account = FakeAccount(tmp_path, plan)
    manager = LiveIngestManager(account)
    try:
        await collect(manager, min_bytes=1000)
    finally:
        manager.stop_all()
    assert account.relogins == 1


async def test_repeated_auth_errors_trip_to_needs_login(tmp_path):
    account = FakeAccount(
        tmp_path, [lambda: FakeSession([], raise_on_start=AuthenticationError("bad"))]
    )
    manager = LiveIngestManager(account)
    ingest = await manager.ensure(SID)
    try:
        for _ in range(100):
            if ingest.state == "failed":
                break
            await asyncio.sleep(0.1)
        assert ingest.state == "failed"
        assert account.state.status == "needs_attention"
        opened = account.opened
        await asyncio.sleep(0.3)
        assert account.opened == opened, "no retries once the account needs login"
    finally:
        manager.stop_all()


async def test_captcha_stops_immediately(tmp_path):
    account = FakeAccount(
        tmp_path, [lambda: FakeSession([], raise_on_start=CaptchaRequiredError("captcha"))]
    )
    manager = LiveIngestManager(account)
    ingest = await manager.ensure(SID)
    try:
        for _ in range(50):
            if ingest.state == "failed":
                break
            await asyncio.sleep(0.1)
        assert ingest.state == "failed"
        assert account.opened == 1
    finally:
        manager.stop_all()


async def test_offline_result_marks_offline_and_retries(tmp_path):
    account = FakeAccount(tmp_path, [lambda: FakeSession([], result=5404)])
    manager = LiveIngestManager(account)
    ingest = await manager.ensure(SID)
    try:
        for _ in range(50):
            if account.opened >= 3:
                break
            await asyncio.sleep(0.1)
        assert account.opened >= 3
        assert ingest.state == "offline"
    finally:
        manager.stop_all()


async def test_idle_stop_terminates_ffmpeg(tmp_path, ps_chunks):
    account = FakeAccount(tmp_path, [lambda: FakeSession(ps_chunks, block_after=True)])
    manager = LiveIngestManager(account, idle_stop_seconds=0.3)
    await collect(manager, min_bytes=1000)
    ingest = manager._ingests[SID]
    proc = ingest._ff
    assert proc is not None
    await asyncio.sleep(1.5)
    assert manager.status() == {}
    assert proc.poll() is not None
    assert not any(t.is_alive() for t in ingest._threads)


async def test_stall_forces_reconnect(tmp_path, ps_chunks):
    plan = [
        lambda: FakeSession(ps_chunks[:5], block_after=True),
        lambda: FakeSession(ps_chunks, block_after=True),
    ]
    account = FakeAccount(tmp_path, plan)
    manager = LiveIngestManager(account, stall_seconds=0.5)
    ingest = await manager.ensure(SID)
    try:
        for _ in range(100):
            if account.opened >= 2:
                break
            await asyncio.sleep(0.1)
        assert account.opened >= 2
        assert ingest.reconnects >= 1
    finally:
        manager.stop_all()


async def test_force_reconnect_keeps_ffmpeg(tmp_path, ps_chunks):
    account = FakeAccount(tmp_path, [lambda: FakeSession(ps_chunks, block_after=True)])
    manager = LiveIngestManager(account)
    await collect(manager, min_bytes=1000)
    ingest = manager._ingests[SID]
    proc = ingest._ff
    assert manager.force_reconnect(SID)
    for _ in range(100):
        if account.opened >= 2:
            break
        await asyncio.sleep(0.1)
    try:
        assert account.opened >= 2
        assert ingest._ff is proc
        assert manager.force_reconnect("nope") is False
    finally:
        manager.stop_all()


async def test_unknown_stream_and_stream_cap(tmp_path, ps_chunks):
    account = FakeAccount(tmp_path, [lambda: FakeSession(ps_chunks, block_after=True)])
    manager = LiveIngestManager(account, max_streams=0)
    with pytest.raises(UnknownStream):
        await manager.find_camera("missing_1")
    with pytest.raises(TooManyStreams):
        await manager.ensure(SID)


async def test_rotation_reopens_before_the_cloud_cutoff(tmp_path, ps_chunks):
    account = FakeAccount(tmp_path, [lambda: FakeSession(ps_chunks, block_after=True)])
    manager = LiveIngestManager(account, rotate_seconds=0.6, stall_seconds=5)
    await collect(manager, min_bytes=1000)
    ingest = manager.status()
    proc = manager._ingests[SID]._ff
    try:
        for _ in range(100):
            if account.opened >= 3:
                break
            await asyncio.sleep(0.1)
        assert account.opened >= 3
        health = manager.status()[SID]
        assert health["rotations"] >= 2 and ingest[SID]["rotations"] == 0
        assert manager._ingests[SID]._ff is proc, "rotation keeps the same ffmpeg"
    finally:
        manager.stop_all()


async def test_two_second_stall_reconnects_quickly(tmp_path, ps_chunks):
    plan = [
        lambda: FakeSession(ps_chunks[:5], block_after=True),
        lambda: FakeSession(ps_chunks, block_after=True),
    ]
    account = FakeAccount(tmp_path, plan)
    manager = LiveIngestManager(account, stall_seconds=2.0, rotate_seconds=999)
    await manager.ensure(SID)
    started = asyncio.get_running_loop().time()
    try:
        while account.opened < 2:
            await asyncio.sleep(0.05)
        assert asyncio.get_running_loop().time() - started < 4.5
    finally:
        manager.stop_all()


async def test_missing_vtm_resource_is_transient_not_offline(tmp_path, ps_chunks):
    from hikcloudstream.exceptions import StreamNegotiationError

    def flaky():
        raise StreamNegotiationError("could not find VTM resource for SER1 channel 1")

    account = FakeAccount(tmp_path, [flaky, lambda: FakeSession(ps_chunks)])
    manager = LiveIngestManager(account)
    ingest = await manager.ensure(SID)
    try:
        await asyncio.sleep(0.3)
        assert ingest.state != "offline"
        await collect(manager, min_bytes=1000)
        assert account.opened >= 2
    finally:
        manager.stop_all()

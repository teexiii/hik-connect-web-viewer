import asyncio

import pytest

from camviewer.clip_recorder import ClipBusy, ClipRecorder
from tests.conftest import ORIGIN


def test_camera_list_shape(logged_in):
    client, _, _ = logged_in
    assert client.get("/api/cameras").json() == [
        {
            "id": "SER1_1",
            "name": "Cam One",
            "serial": "SER1",
            "channel": 1,
            "online": None,
            "streams": {"hevc": "SER1_1", "h264": "SER1_1_h264"},
        }
    ]


def test_status_reports_account_and_streams(logged_in):
    client, _, _ = logged_in
    body = client.get("/api/status").json()
    assert body["account"] == {"status": "ok", "username": "tee"}
    assert body["streams"] == {}


def test_thumbnail_and_unknown_camera(logged_in):
    client, _, _ = logged_in
    response = client.get("/api/cameras/SER1_1/thumbnail")
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    assert client.get("/api/cameras/NOPE_9/thumbnail").status_code == 404


def test_reconnect_for_inactive_stream_reports_not_active(logged_in):
    client, _, _ = logged_in
    response = client.post("/api/streams/SER1_1/reconnect", headers=ORIGIN)
    assert response.json() == {"ok": True, "active": False}
    assert client.post("/api/streams/NOPE_9/reconnect", headers=ORIGIN).status_code == 404


def test_clip_validation_and_unknown_job(logged_in):
    client, _, _ = logged_in
    short = client.post("/api/cameras/SER1_1/clip", json={"seconds": 5}, headers=ORIGIN)
    assert short.status_code == 422
    long = client.post("/api/cameras/SER1_1/clip", json={"seconds": 500}, headers=ORIGIN)
    assert long.status_code == 422
    assert client.get("/api/clips/" + "a" * 32).status_code == 404
    assert client.get("/api/clips/..%2F..%2Fetc%2Fpasswd/download").status_code == 404


async def test_clip_recorder_limits_and_cleanup(tmp_path):
    recorder = ClipRecorder(tmp_path, "rtsp://127.0.0.1:1", ffmpeg_path="nonexistent-ffmpeg")
    first = recorder.start("SER1_1", 10)
    recorder.start("SER1_1", 10)
    with pytest.raises(ClipBusy):
        recorder.start("SER1_1", 10)
    with pytest.raises(ValueError):
        recorder.start("SER1_1", 9)
    await asyncio.sleep(0.3)  # missing ffmpeg binary -> job fails
    assert recorder.status(first)["status"] == "failed"
    assert recorder.path_for(first) is None

    clips = tmp_path / "clips"
    clips.mkdir(exist_ok=True)
    for i in range(25):
        (clips / f"{i:032x}.mp4").write_bytes(b"x")
    recorder.cleanup()
    assert len(list(clips.glob("*.mp4"))) <= 19

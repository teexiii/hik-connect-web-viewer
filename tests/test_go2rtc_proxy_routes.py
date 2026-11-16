import asyncio
import threading

import pytest
import respx
import websockets
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from camviewer.session_auth import COOKIE_NAME

UPSTREAM = "http://127.0.0.1:1984"
ORIGIN_HDR = {"origin": "http://testserver"}


@respx.mock
def test_frame_forwards_only_the_validated_src(logged_in):
    client, _, _ = logged_in
    route = respx.get(f"{UPSTREAM}/api/frame.jpeg").respond(200, content=b"\xff\xd8jpg")
    response = client.get("/go2rtc/api/frame.jpeg?src=SER1_1_h264&dst=x&mp4=1")
    assert response.status_code == 200 and response.content == b"\xff\xd8jpg"
    assert dict(route.calls.last.request.url.params) == {"src": "SER1_1_h264"}


@respx.mock
@pytest.mark.parametrize("src", ["exec:sh -c id", "ffmpeg:http://192.168.1.1/x", "other", ""])
@pytest.mark.parametrize("path", ["frame.jpeg", "stream.m3u8"])
def test_foreign_src_is_rejected_on_every_http_route(logged_in, path, src):
    client, _, _ = logged_in
    route = respx.get(url__startswith=UPSTREAM).respond(200)
    assert client.get(f"/go2rtc/api/{path}", params={"src": src}).status_code == 403
    assert client.get(f"/go2rtc/api/{path}").status_code == 403
    assert not route.called


@respx.mock
def test_hls_files_need_a_clean_id_and_name(logged_in):
    client, _, _ = logged_in
    route = respx.get(f"{UPSTREAM}/api/hls/segment.ts").respond(200, content=b"ts")
    assert client.get("/go2rtc/api/hls/segment.ts?id=abc123&src=exec:x").status_code == 200
    assert dict(route.calls.last.request.url.params) == {"id": "abc123"}
    assert client.get("/go2rtc/api/hls/segment.ts?id=a/b").status_code == 404
    assert client.get("/go2rtc/api/hls/segment.ts").status_code == 404


def test_other_go2rtc_routes_are_not_proxied(logged_in):
    client, _, _ = logged_in
    for path in ("streams", "config", "exec", "stream.mp4"):
        assert client.get(f"/go2rtc/api/{path}").status_code == 404


class FakeGo2rtcWs:
    """Echo websocket on an ephemeral port, run in its own thread and loop."""

    def __init__(self):
        self.seen_paths = []
        self.port = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    async def _handler(self, ws):
        self.seen_paths.append(ws.request.path)
        async for message in ws:
            await ws.send(message)

    def _run(self):
        async def main():
            async with websockets.serve(self._handler, "127.0.0.1", 0) as server:
                self.port = server.sockets[0].getsockname()[1]
                self._ready.set()
                await asyncio.Future()

        asyncio.run(main())

    def start(self):
        self._thread.start()
        self._ready.wait(5)


@pytest.fixture
def fake_ws():
    server = FakeGo2rtcWs()
    server.start()
    return server


def _point_proxy_at(app, port):
    object.__setattr__(app.state.ctx.settings, "go2rtc_url", f"http://127.0.0.1:{port}")


def test_ws_proxy_relays_both_ways_for_allowed_src(logged_in, fake_ws):
    client, app, _ = logged_in
    _point_proxy_at(app, fake_ws.port)
    with client.websocket_connect("/go2rtc/api/ws?src=SER1_1", headers=ORIGIN_HDR) as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "hello"
        ws.send_bytes(b"\x01\x02")
        assert ws.receive_bytes() == b"\x01\x02"
    assert fake_ws.seen_paths == ["/api/ws?src=SER1_1"]


@pytest.mark.parametrize("query", ["src=exec:id", "src=ffmpeg:http://x", ""])
def test_ws_proxy_rejects_foreign_src(logged_in, fake_ws, query):
    client, app, _ = logged_in
    _point_proxy_at(app, fake_ws.port)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/go2rtc/api/ws?{query}", headers=ORIGIN_HDR):
            pass
    assert fake_ws.seen_paths == []


def test_ws_proxy_requires_session_and_origin(logged_in, fake_ws):
    client, app, _ = logged_in
    _point_proxy_at(app, fake_ws.port)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/go2rtc/api/ws?src=SER1_1", headers={"origin": "http://evil.example"}
        ):
            pass
    cookie = client.cookies.get(COOKIE_NAME)
    anonymous = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with anonymous.websocket_connect("/go2rtc/api/ws?src=SER1_1", headers=ORIGIN_HDR):
            pass
    anonymous.cookies.set(COOKIE_NAME, cookie)
    with anonymous.websocket_connect("/go2rtc/api/ws?src=SER1_1", headers=ORIGIN_HDR) as ws:
        ws.send_text("ok")
        assert ws.receive_text() == "ok"

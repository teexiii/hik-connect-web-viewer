import httpx
import respx
from fastapi.testclient import TestClient

from camviewer.go2rtc_client import Go2RtcClient
from camviewer.internal_routes import create_internal_app

API = "http://127.0.0.1:1984"


@respx.mock
async def test_reconcile_registers_only_missing_streams():
    respx.get(f"{API}/api/streams").respond(200, json={"SER1_1": {}})
    put = respx.put(f"{API}/api/streams").respond(200)
    async with httpx.AsyncClient() as http:
        client = Go2RtcClient(API, "http://127.0.0.1:8081", http)
        assert await client.reconcile(["SER1_1"]) == 1
    params = dict(put.calls.last.request.url.params)
    assert params == {"name": "SER1_1_h264", "src": "http://127.0.0.1:8081/internal/h264/SER1_1"}


@respx.mock
async def test_reconcile_registers_ts_source_for_the_base_stream():
    respx.get(f"{API}/api/streams").respond(200, json={})
    put = respx.put(f"{API}/api/streams").respond(200)
    async with httpx.AsyncClient() as http:
        client = Go2RtcClient(API, "http://127.0.0.1:8081", http)
        assert await client.reconcile(["SER1_1"]) == 2
    first = dict(put.calls[0].request.url.params)
    assert first == {"name": "SER1_1", "src": "http://127.0.0.1:8081/internal/ts/SER1_1"}


@respx.mock
async def test_reconcile_survives_go2rtc_being_down():
    respx.get(f"{API}/api/streams").mock(side_effect=httpx.ConnectError("down"))
    async with httpx.AsyncClient() as http:
        client = Go2RtcClient(API, "http://127.0.0.1:8081", http)
        assert await client.reconcile(["SER1_1"]) == 0


def test_internal_ts_rejects_unknown_stream_and_unready_account(make_app):
    public, account = make_app()
    internal = TestClient(create_internal_app(public))  # no lifespan: no background login
    assert internal.get("/healthz").status_code == 200
    assert internal.get("/internal/ts/SER1_1").status_code == 503  # account not ready yet
    account.login("hikuser", "hikpw")
    assert internal.get("/internal/ts/NOPE_9").status_code == 404
    assert internal.get("/internal/h264/NOPE_9").status_code == 404

# camviewer

Mobile-first web viewer for Hik-Connect cloud cameras: log in with a Hik-Connect account, see a camera grid, tap for fullscreen live view, take snapshots and record clips. Live video reconnects on its own when the cloud session times out.

## Read this first

- It talks to Hik-Connect through the **unofficial** library [`hikcloudstream`](https://pypi.org/project/hikcloudstream/) (version pinned in `uv.lock`). This is not an approved Hikvision integration and may break or violate the service terms. Use it only with your own account and cameras.
- The Hik-Connect password is stored on the server, encrypted with a Fernet key (`CAMVIEWER_SECRET_KEY`) in `DATA_DIR/credentials.enc`. Anyone with both the file and the key can read it. Use LAN-only access; do not expose the port to the internet without HTTPS and an auth layer (the session cookie is not `Secure` on plain HTTP).
- Cameras that sit on Wi-Fi without LAN RTSP are reached through Hik-Connect's cloud relay (VTM). Only the MAIN stream carries data on the tested camera (HEVC 1280x720, 15 fps).

## How it works

```
phone --> app :8080 (login, API, static UI, allowlisted proxy) --> go2rtc (loopback only)
              |                                                       | pulls http://127.0.0.1:8081/internal/ts/{id}
              +-- ingest per camera: Hik-Connect VTM session -> one long-lived ffmpeg -> MPEG-TS
```

- One Hik-Connect login is shared by all cameras; ingest starts when the first viewer connects and stops 30 s after the last one leaves (`MAX_STREAMS`, default 3).
- Reconnect layers: the ingest rotates the cloud session every 225 s (the cloud stops sending data around 240 s) and reopens within 2 s of a stall while keeping the same ffmpeg; go2rtc re-pulls the source; the browser watchdog restarts the player when the picture freezes.
- The browser plays HEVC directly when it can (`{id}` stream) and falls back to a go2rtc H.264 transcode (`{id}_h264`). Add `?stream=hevc` or `?stream=h264` to the page URL to force one.
- go2rtc listens on `127.0.0.1` only (its API can create `exec:` sources). The app proxies just the allowlisted routes and stream names after checking the session cookie and `Origin`.

## Run locally (Docker)

```bash
cp .env.example .env        # fill CAMVIEWER_SECRET_KEY and SESSION_SECRET (commands inside)
docker compose up --build
# open http://localhost:8080 and log in with the Hik-Connect account
```

Without Docker, install `ffmpeg`, run go2rtc yourself with `go2rtc.yaml`, then:

```bash
uv sync
uv run python -m camviewer.main     # needs the two secrets in the environment
```

## Test

```bash
uv run pytest
uv run ruff check
```

## Deploy to k3s (192.168.1.2)

`deploy/k3s/deploy.sh` builds the image, ships it with the go2rtc image into k3s containerd, creates the secrets on the server on first run (never printed), applies `deploy/k3s/*.yaml` and waits for the rollout. The Pod holds both containers on one network namespace; the Service is a NodePort on **30092** (LAN only).

```bash
bash deploy/k3s/deploy.sh
curl http://192.168.1.2:30092/healthz
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CAMVIEWER_SECRET_KEY` | required | Fernet key for the credential file |
| `SESSION_SECRET` | required | signs session cookies |
| `DATA_DIR` | `data` | credentials, state, clips |
| `MAX_STREAMS` | `3` | concurrent camera ingests |
| `IDLE_STOP_SECONDS` | `30` | stop an ingest after the last viewer leaves |
| `STALL_SECONDS` | `2` | no data for this long reopens the cloud session |
| `SESSION_ROTATE_SECONDS` | `225` | proactively rotate the cloud session |
| `GO2RTC_URL`, `GO2RTC_RTSP_URL`, `INTERNAL_TS_BASE` | loopback defaults | internal endpoints |

## Troubleshooting

- **"Hik-Connect yêu cầu CAPTCHA" / HTTP 423**: open the official Hik-Connect app, log in once, then retry here. Repeated failed logins cause this; the app stops retrying after three auth failures and shows the login screen.
- **Password changed in the app**: the stored password is rejected, the UI returns to the login page. Log in again with the new password (same account) to replace it.
- **Camera shows offline**: the cloud reports no stream (session result 5404). The ingest backs off up to 60 s; check the camera's power and Wi-Fi.
- **Video stuck on "Đang kết nối"**: try "Làm mới"; check `/api/status` for the stream state and `docker compose logs app`.
- **Lost the Fernet key / switched accounts**: use the menu "Xoá tài khoản khỏi máy chủ" or delete `credentials.enc` from the data volume.

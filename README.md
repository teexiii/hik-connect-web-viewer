# camviewer

Mobile-first web viewer for Hik-Connect cloud cameras: sign in to the web app, land in the live view (grid only when more than one camera is online), take snapshots and record clips. Live video reconnects on its own when the cloud session times out.

## Read this first

- It talks to Hik-Connect through the **unofficial** library [`hikcloudstream`](https://pypi.org/project/hikcloudstream/) (version pinned in `uv.lock`). This is not an approved Hikvision integration and may break or violate the service terms. Use it only with your own account and cameras.
- Two separate accounts:
  - the **app login** (`APP_USERNAME` + `APP_PASSWORD_HASH`) protects this website;
  - the **Hik-Connect account** (`HIK_CONNECT_USER` + `HIK_CONNECT_PASSWORD`) is what the server logs in with at startup. Both live only in environment variables / the k8s Secret, never in the repo.
- The session cookie (`camviewer_session`, HttpOnly, SameSite=Lax, `Secure` unless `COOKIE_SECURE=false`) lasts 400 days and is re-issued on every request, so an active browser never has to log in again. "Đăng xuất" revokes every issued cookie.
- Cameras on Wi-Fi without LAN RTSP are reached through Hik-Connect's cloud relay. Only the MAIN stream carries data on the tested camera (HEVC 1280x720, 15 fps).

## How it works

```
phone --> app :8080 (login, API, static UI, allowlisted proxy) --> go2rtc (loopback only)
              |                                                       | pulls http://127.0.0.1:8081/internal/ts/{id}
              +-- ingest per camera: Hik-Connect VTM session -> one long-lived ffmpeg -> MPEG-TS
```

- One Hik-Connect login is shared by all cameras; an ingest starts when the first viewer connects and stops 30 s after the last one leaves (`MAX_STREAMS`, default 3).
- Reconnect layers: the ingest rotates the cloud session every 150 s (the cloud stops sending data at about 240 s, and a session that old makes the camera refuse new sessions for 90-160 s; sessions up to 200 s reopen instantly) and reopens within 2 s of a stall while keeping the same ffmpeg; go2rtc re-pulls the source; the browser watchdog restarts the player when the picture freezes.
- Every browser gets an H.264 transcode (`{id}_h264`, pinned to 1280x720) made by the app's own ffmpeg: the cloud switches resolution mid-stream, which broke HEVC passthrough on iPhone (green frame, then crash), and go2rtc's bundled ffmpeg 8 mis-decodes this camera's HEVC. `?stream=hevc` forces passthrough for debugging.
- Playback prefers WebRTC (go2rtc media on hostPort 8555 TCP+UDP, candidate `192.168.1.2:8555`, signaling through the app's authenticated WebSocket) and falls back to MSE. Off the LAN WebRTC needs a router forward of 8555 to 192.168.1.2; without it the player stays on MSE. The watchdog counts painted frames (`requestVideoFrameCallback`) because a WebRTC stream's clock keeps running while the picture is frozen.
- go2rtc listens on `127.0.0.1` only (its API can create `exec:` sources). The app proxies just the allowlisted routes and stream names after checking the session cookie and `Origin`.
- Hik-Connect failures: three auth failures in a row or a CAPTCHA stop all retries and show a banner with a "Thử lại" button (it re-runs the login). A temporarily unreachable cloud is retried with back-off.

## Run locally (Docker)

```bash
cp .env.example .env
uv run python -m camviewer.hash_password     # paste the output as APP_PASSWORD_HASH (single-quoted in .env)
# fill APP_USERNAME, SESSION_SECRET, HIK_CONNECT_*; set COOKIE_SECURE=false for plain http
docker compose up --build
# open http://localhost:8080 (or HOST_PORT)
```

Without Docker: install `ffmpeg`, run go2rtc yourself with `go2rtc.yaml`, export the same variables and run `uv run python -m camviewer.main`.

## Test

```bash
uv run pytest
uv run ruff check
```

## Deploy to k3s (192.168.1.2) behind Cloudflare

Public URL `https://cam.ult.vn` (wildcard DNS, not Cloudflare-proxied) terminates TLS at the host nginx (Webinoly site, Let's Encrypt), which proxies to the NodePort. Its `apps.d/cam.ult.vn-proxy.conf` uses `location ^~ /` so Webinoly's static/deny regex locations never take app paths, passes `Host`, `X-Real-IP`, `X-Forwarded-*` and WebSocket upgrade headers (the `Origin` check compares `Origin` to `Host`), and has long read timeouts for live video.

```bash
# secrets file lives outside the repo, plain KEY=value lines, no quotes:
#   SESSION_SECRET, APP_USERNAME, APP_PASSWORD_HASH, HIK_CONNECT_USER, HIK_CONNECT_PASSWORD
CAMVIEWER_SECRETS_FILE=~/secrets/camviewer.env bash deploy/k3s/deploy.sh
```

`deploy.sh` builds the image, ships it with the go2rtc image into k3s containerd, turns the secrets file into the `camviewer-secrets` Secret on the server (then deletes the copy), applies `deploy/k3s/*.yaml` and waits for the rollout. The Pod holds both containers on one network namespace; the Service is a NodePort on **30092**. Without `CAMVIEWER_SECRETS_FILE` the existing Secret is reused.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `APP_USERNAME`, `APP_PASSWORD_HASH` | required | app login (hash from `python -m camviewer.hash_password`) |
| `SESSION_SECRET` | required | signs session cookies |
| `HIK_CONNECT_USER`, `HIK_CONNECT_PASSWORD` | none | Hik-Connect account used by the server |
| `COOKIE_SECURE` | `true` | set `false` for plain-http local runs |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | proxies trusted for forwarded headers (`*` in the k3s manifest) |
| `DATA_DIR` | `data` | state and clips |
| `MAX_STREAMS` | `3` | concurrent camera ingests |
| `IDLE_STOP_SECONDS` | `30` | stop an ingest after the last viewer leaves |
| `STALL_SECONDS` | `2` | no data for this long reopens the cloud session |
| `SESSION_ROTATE_SECONDS` | `150` | proactively rotate the cloud session |
| `GO2RTC_URL`, `INTERNAL_TS_BASE` | loopback defaults | internal endpoints |

Login attempts are limited to 10 per 5 minutes overall and 5 per 5 minutes per client IP (`X-Real-IP` from the host nginx, which overwrites it with `$remote_addr`).

## Troubleshooting

- **Banner "Hik-Connect yêu cầu CAPTCHA"**: open the official Hik-Connect app, log in once, then press "Thử lại". Repeated failed logins cause this.
- **Banner "Hik-Connect từ chối tài khoản"**: the password in the Secret is wrong or was changed. Update the Secret, restart the pod (`kubectl -n camviewer rollout restart deployment/camviewer`).
- **Camera missing from the grid**: cameras the cloud reports offline are hidden and listed under the "⋯" menu.
- **Video stuck on "Đang kết nối"**: try "Làm mới"; check `/api/status` for the stream state and the app logs.

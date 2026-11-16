"""Spike: validate hikcloudstream against a real Hik-Connect account.

Reads HIK_CONNECT_USER / HIK_CONNECT_PASSWORD (and optional HIK_VALIDATE_CODE)
from the environment or from an env file given with --env-file. Never prints
the password. Writes artefacts to spike-out/ (gitignored).

Usage:
  uv run python scripts/spike_hikcloudstream.py --env-file PATH [--soak-minutes 20]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from hikcloudstream import Credentials, HikConnectClient, StreamType
from hikcloudstream.stream import open_live_stream
from pyezvizapi.stream import StreamTransport, detect_transport

OUT = Path("spike-out")


def load_env_file(path: str | None) -> None:
    if not path:
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def live_probe(client: HikConnectClient, cam, stream_type: StreamType, seconds: float) -> None:
    """Read a live stream for `seconds`, remux to MPEG-TS, report bytes and transport."""
    t0 = time.monotonic()
    with open_live_stream(client, cam, stream_type=stream_type) as session:
        info = session.start()
        log(f"  {stream_type.name}: start result={getattr(info, 'result', None)} "
            f"selected={session.stream_type} open={time.monotonic() - t0:.1f}s")
        transport = None
        total = 0
        first_at = None
        out_raw = OUT / f"cam-{stream_type.name.lower()}.bin"
        with out_raw.open("wb") as fh:
            for chunk in session.iter_annex_b_chunks(duration_seconds=seconds):
                if first_at is None:
                    first_at = time.monotonic() - t0
                    transport = detect_transport(chunk)
                total += len(chunk)
                fh.write(chunk)
    log(f"  {stream_type.name}: bytes={total} first_byte={first_at}s transport={transport}")
    fmt = "mpeg" if transport == StreamTransport.MPEG_PS else "h264"
    ts = OUT / f"cam-{stream_type.name.lower()}.ts"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", fmt,
                    "-i", str(out_raw), "-c", "copy", "-f", "mpegts", str(ts)], check=False)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "stream=codec_name,width,height,r_frame_rate:format=duration",
                            "-of", "compact", str(ts)], capture_output=True, text=True)
    log(f"  {stream_type.name}: ffprobe raw-fmt={fmt}: {probe.stdout.strip() or probe.stderr.strip()}")


def soak(client: HikConnectClient, cam, minutes: float) -> None:
    """Hold one SUB session open; log when/how it ends; then measure reopen time."""
    deadline = time.monotonic() + minutes * 60
    rounds = 0
    while time.monotonic() < deadline and rounds < 4:
        rounds += 1
        t_open = time.monotonic()
        total = 0
        last_report = t_open
        reason = "iterator ended"
        try:
            with open_live_stream(client, cam, stream_type=StreamType.MAIN) as session:
                session.start()
                for chunk in session.iter_annex_b_chunks():
                    total += len(chunk)
                    now = time.monotonic()
                    if now - last_report > 60:
                        log(f"  soak r{rounds}: alive {(now - t_open) / 60:.1f}min bytes={total}")
                        last_report = now
                    if now > deadline:
                        reason = "soak deadline"
                        break
        except Exception as exc:  # noqa: BLE001 - spike wants every failure mode
            reason = f"{type(exc).__name__}: {exc}"
        log(f"  soak r{rounds}: session lasted {(time.monotonic() - t_open):.0f}s "
            f"bytes={total} end={reason}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file")
    ap.add_argument("--soak-minutes", type=float, default=0)
    ap.add_argument("--probe-seconds", type=float, default=15)
    ap.add_argument("--camera", type=int, default=1, help="1-based camera index")
    args = ap.parse_args()
    load_env_file(args.env_file)
    OUT.mkdir(exist_ok=True)

    with HikConnectClient() as client:
        t0 = time.monotonic()
        client.login(Credentials(os.environ["HIK_CONNECT_USER"], os.environ["HIK_CONNECT_PASSWORD"]))
        log(f"a. login OK in {time.monotonic() - t0:.1f}s")
        cams = client.list_cameras()
        log(f"a. cameras: {len(cams)}")
        for c in cams:
            log(f"   #{c.index} name={c.name!r} device={c.device_name!r} serial={c.device_serial} ch={c.channel_no}")
        if not cams:
            return 1
        cam = cams[args.camera - 1]
        log(f"   using #{cam.index} {cam.name!r}")
        try:
            jpg = client.capture_snapshot(cam, validate_code=os.environ.get("HIK_VALIDATE_CODE"))
            (OUT / "thumb.jpg").write_bytes(jpg)
            log(f"b. cloud snapshot OK {len(jpg)} bytes")
        except Exception as exc:  # noqa: BLE001
            log(f"b. cloud snapshot FAIL {type(exc).__name__}: {exc}")

        for st in (StreamType.SUB, StreamType.MAIN):
            try:
                live_probe(client, cam, st, args.probe_seconds)
            except Exception as exc:  # noqa: BLE001
                log(f"c. live {st.name} FAIL {type(exc).__name__}: {exc}")

        # f. two concurrent sessions (sub + main)
        results: dict[str, str] = {}

        def worker(st: StreamType) -> None:
            try:
                with open_live_stream(client, cam, stream_type=st) as s:
                    s.start()
                    n = sum(len(c) for c in s.iter_annex_b_chunks(duration_seconds=8))
                    results[st.name] = f"bytes={n}"
            except Exception as exc:  # noqa: BLE001
                results[st.name] = f"{type(exc).__name__}: {exc}"

        threads = [threading.Thread(target=worker, args=(st,)) for st in (StreamType.SUB, StreamType.MAIN)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        log(f"f. concurrent sub+main: {results}")

        if args.soak_minutes:
            log(f"d/e. soak {args.soak_minutes} min")
            soak(client, cam, args.soak_minutes)
    return 0


if __name__ == "__main__":
    sys.exit(main())

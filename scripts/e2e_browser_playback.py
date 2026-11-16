"""Local E2E: real Chrome (headless) plays the grid tile; measures frozen-picture gaps.

Usage:
  uv run --with playwright python scripts/e2e_browser_playback.py BASE COOKIE_JSON CAMERA_ID \
      --stream h264|hevc --baseline 20 --reconnects 3 --soak-seconds 0 [--outdir DIR]

Needs Google Chrome installed (playwright channel "chrome": H.264/HEVC capable).
Reports gaps as "longest window where video.currentTime did not advance".
"""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

SAMPLER = """
(cameraId) => {
  window.__samples = [];
  setInterval(() => {
    const v = document.querySelector('#viewer:not([hidden]) video') ||
      document.querySelector(`.card[data-id="${cameraId}"] video`);
    window.__samples.push([Date.now(), v ? v.currentTime : -1]);
  }, 250);
}
"""


def frozen_windows(samples, since_ms):
    """Longest run (seconds) of non-advancing currentTime among samples after since_ms."""
    longest, run_start, last = 0.0, None, None
    for ts, t in samples:
        if ts < since_ms:
            continue
        if last is not None and t <= last + 0.001:
            run_start = run_start or prev_ts
            longest = max(longest, (ts - run_start) / 1000)
        else:
            run_start = None
        last, prev_ts = t, ts
    return round(longest, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("cookie_json")
    ap.add_argument("camera_id")
    ap.add_argument("--stream", default="h264")
    ap.add_argument("--baseline", type=float, default=20)
    ap.add_argument("--reconnects", type=int, default=3)
    ap.add_argument("--soak-seconds", type=float, default=0)
    ap.add_argument("--outdir", default="spike-out")
    args = ap.parse_args()
    out = Path(args.outdir)
    out.mkdir(exist_ok=True)
    cookies = json.loads(Path(args.cookie_json).read_text(encoding="utf-8"))
    host = urlparse(args.base).hostname
    result = {"stream": args.stream}

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        context = browser.new_context(**p.devices["Pixel 7"], accept_downloads=True)
        context.add_cookies([{"name": k, "value": v, "domain": host, "path": "/"} for k, v in cookies.items()])
        page = context.new_page()
        page.on("console", lambda m: print(time.strftime("%H:%M:%S"), "console:", m.text[:160]) if m.type in ("error", "warning") or "[watchdog]" in m.text else None)
        page.goto(args.base + ("/" if args.stream == "auto" else f"/?stream={args.stream}"))
        page.evaluate(SAMPLER, args.camera_id)

        t0 = time.time()
        while time.time() - t0 < 60:
            samples = page.evaluate("window.__samples")
            if samples and samples[-1][1] > 1:
                break
            time.sleep(0.5)
        result["time_to_first_frames_s"] = round(time.time() - t0, 1)
        time.sleep(args.baseline)
        samples = page.evaluate("window.__samples")
        result["advancing"] = samples[-1][1] > samples[0][1] + args.baseline / 2
        result["baseline_gap_s"] = frozen_windows(samples, samples[0][0] + 5000)
        page.screenshot(path=str(out / f"grid-{args.stream}.png"))

        gaps = []
        for i in range(args.reconnects):
            mark = int(time.time() * 1000)
            status = page.evaluate(
                "(id) => fetch(`/api/streams/${id}/reconnect`, {method:'POST', credentials:'same-origin'}).then(r => r.status)",
                args.camera_id)
            time.sleep(12)
            gaps.append({"api_status": status, "gap_s": frozen_windows(page.evaluate("window.__samples"), mark)})
            print("reconnect", i + 1, gaps[-1], flush=True)
        result["reconnects"] = gaps
        result["still_playing"] = page.evaluate("window.__samples")[-1][1] > 0

        if args.soak_seconds:
            mark = int(time.time() * 1000)
            time.sleep(args.soak_seconds)
            samples = page.evaluate("window.__samples")
            result["soak_max_gap_s"] = frozen_windows(samples, mark)
            status = page.evaluate("fetch('/api/status').then(r => r.json())")
            result["soak_ingest"] = status["streams"]

        # snapshot + 10 s clip through the authenticated API from inside the page
        snap = page.evaluate(
            "(id) => fetch(`/api/cameras/${id}/snapshot`).then(async r => { "
            "const bytes = new Uint8Array(await r.arrayBuffer()); let bin = ''; "
            "for (const b of bytes) bin += String.fromCharCode(b); return [r.status, btoa(bin)]; })",
            args.camera_id)
        result["snapshot_status"] = snap[0]
        (out / "snapshot.jpg").write_bytes(__import__("base64").b64decode(snap[1]))
        clip = page.evaluate(
            """async (id) => {
              const post = await fetch(`/api/cameras/${id}/clip`, {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify({seconds: 10})});
              const {job_id} = await post.json();
              for (let i = 0; i < 40; i++) {
                await new Promise(r => setTimeout(r, 1500));
                const job = await (await fetch(`/api/clips/${job_id}`)).json();
                if (job.status === 'done') {
                  const file = await fetch(`/api/clips/${job_id}/download`);
                  const bytes = new Uint8Array(await file.arrayBuffer());
                  let bin = ''; for (const b of bytes) bin += String.fromCharCode(b);
                  return [file.status, btoa(bin)];
                }
                if (job.status === 'failed') return [0, ''];
              }
              return [-1, ''];
            }""",
            args.camera_id)
        result["clip_status"] = clip[0]
        if clip[1]:
            (out / "clip.mp4").write_bytes(__import__("base64").b64decode(clip[1]))
        browser.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

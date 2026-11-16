"""Background clip recording from the internal MPEG-TS source into DATA_DIR/clips."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("camviewer.clips")

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MIN_SECONDS, MAX_SECONDS = 10, 120
MAX_FILES = 20
MAX_AGE_SECONDS = 24 * 3600
MAX_PARALLEL = 2


class ClipBusy(Exception):
    """Too many recordings already running."""


class ClipRecorder:
    def __init__(self, data_dir: Path, ts_base: str, ffmpeg_path: str = "ffmpeg") -> None:
        self._dir = data_dir / "clips"
        self._ts_base = ts_base.rstrip("/")
        self._ffmpeg = ffmpeg_path
        self._jobs: dict[str, dict[str, Any]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, stream_id: str, seconds: int) -> str:
        if not MIN_SECONDS <= seconds <= MAX_SECONDS:
            raise ValueError("seconds out of range")
        if sum(1 for job in self._jobs.values() if job["status"] == "recording") >= MAX_PARALLEL:
            raise ClipBusy
        self.cleanup()
        job_id = uuid.uuid4().hex
        self._jobs[job_id] = {"status": "recording", "seconds": seconds, "stream": stream_id}
        task = asyncio.create_task(self._record(job_id, stream_id, seconds))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job_id

    async def _record(self, job_id: str, stream_id: str, seconds: int) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        out = self._dir / f"{job_id}.mp4"
        cmd = [
            self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", f"{self._ts_base}/internal/ts/{stream_id}",
            "-t", str(seconds), "-c", "copy", "-tag:v", "hvc1",
            "-movflags", "+faststart", str(out),
        ]  # fmt: skip
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            _, err = await asyncio.wait_for(proc.communicate(), seconds + 45)
            ok = proc.returncode == 0 and out.exists() and out.stat().st_size > 0
            self._jobs[job_id]["status"] = "done" if ok else "failed"
            if not ok:
                log.warning("clip failed: %s", err.decode(errors="replace")[-300:])
        except (TimeoutError, OSError):
            self._jobs[job_id]["status"] = "failed"
            out.unlink(missing_ok=True)

    def status(self, job_id: str) -> dict[str, Any] | None:
        if not JOB_ID_RE.match(job_id):
            return None
        job = self._jobs.get(job_id)
        if job is None:
            return None
        return {"job_id": job_id, **job}

    def path_for(self, job_id: str) -> Path | None:
        job = self.status(job_id)
        if job is None or job["status"] != "done":
            return None
        path = self._dir / f"{job_id}.mp4"
        return path if path.exists() else None

    def cleanup(self) -> None:
        """Drop clips older than 24h and keep at most MAX_FILES newest."""
        if not self._dir.exists():
            return
        files = sorted(self._dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        now = time.time()
        for index, path in enumerate(files):
            if index >= MAX_FILES - 1 or now - path.stat().st_mtime > MAX_AGE_SECONDS:
                path.unlink(missing_ok=True)
                self._jobs.pop(path.stem, None)

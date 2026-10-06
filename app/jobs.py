"""Export jobs: snapshot-bound MP4 rendering with single-terminal-state semantics.

States: ``running`` -> ``done`` | ``cancelled`` | ``failed`` (terminal only).
A cancel that loses the race with completion cannot erase the finished file;
a completion that loses the race is discarded and its temp files removed.
Outputs appear in the exports directory only on success, via a rename — so
a failed or cancelled job never publishes a partial MP4.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import media
from .renderer import Plan
from .state import snapshot_pretty


@dataclass
class Job:
    id: str
    status: str = "running"
    progress: float = 0.0
    message: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    mp4_path: Optional[str] = None
    manifest_path: Optional[str] = None
    snapshot_path: Optional[str] = None
    cancel_flag = False

    def public(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "progress": round(self.progress, 4),
            "message": self.message,
            "mp4_url": f"/exports/{self.id}.mp4" if self.status == "done" else None,
            "manifest_url": f"/exports/{self.id}.mapping.json" if self.status == "done" else None,
            "snapshot_url": f"/exports/{self.id}.snapshot.json" if self.status == "done" else None,
        }


class JobManager:
    def __init__(self, export_dir: Path, data_dir: Path):
        self.export_dir = Path(export_dir)
        self.data_dir = Path(data_dir)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self._lock = threading.RLock()

    def start(self, plan: Plan, snap: dict) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(id=job_id)
        with self._lock:
            self.jobs[job_id] = job
        thread = threading.Thread(
            target=self._run, args=(job, plan, snap), daemon=True
        )
        thread.start()
        return job

    def request_cancel(self, job_id: str) -> Optional[Job]:
        with self._lock:
            job = self.jobs.get(job_id)
            if job and job.status == "running":
                job.cancel_flag = True
                job.message = "cancel requested"
            return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self.jobs.get(job_id)

    # -- worker ----------------------------------------------------------

    def _finalize(self, job: Job, status: str, message: str = "") -> bool:
        """Try to set a terminal state. Returns True if this call won."""
        with self._lock:
            if job.status != "running":
                return False
            job.status = status
            job.message = message
            job.finished_at = time.time()
            if status != "done":
                job.progress = job.progress
            return True

    def _run(self, job: Job, plan: Plan, snap: dict) -> None:
        tmp = self.export_dir / f".{job.id}.tmp"
        try:
            if tmp.exists():
                shutil.rmtree(tmp)
            tmp.mkdir(parents=True)
            self._render(job, plan, snap, tmp)
            # Publish atomically: rename temp outputs into place, then flip
            # state to done.  If a cancel slipped through, drop everything.
            with self._lock:
                if job.cancel_flag or job.status != "running":
                    shutil.rmtree(tmp, ignore_errors=True)
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    return
                mp4_tmp = tmp / "out.mp4"
                manifest_tmp = tmp / "out.mapping.json"
                snapshot_tmp = tmp / "out.snapshot.json"
                mp4_final = self.export_dir / f"{job.id}.mp4"
                manifest_final = self.export_dir / f"{job.id}.mapping.json"
                snapshot_final = self.export_dir / f"{job.id}.snapshot.json"
                mp4_tmp.rename(mp4_final)
                manifest_tmp.rename(manifest_final)
                snapshot_tmp.rename(snapshot_final)
                job.mp4_path = str(mp4_final)
                job.manifest_path = str(manifest_final)
                job.snapshot_path = str(snapshot_final)
                job.status = "done"
                job.progress = 1.0
                job.message = "export finished"
                job.finished_at = time.time()
        except Exception as exc:  # noqa: BLE001 - surfaced through job state
            shutil.rmtree(tmp, ignore_errors=True)
            with self._lock:
                cancelled_now = job.cancel_flag or job.status == "cancelled"
            if cancelled_now and self._finalize(job, "cancelled", "cancelled"):
                return
            detail = "".join(
                traceback.format_exception_only(type(exc), exc)
            ).strip()
            self._finalize(job, "failed", detail)
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    def _render(self, job: Job, plan: Plan, snap: dict, tmp: Path) -> None:
        from .rationals import OUTPUT_FPS, OUTPUT_SAMPLE_RATE

        def cancelled() -> bool:
            return job.cancel_flag

        # 1) Video: write raw RGB, frame by frame, from the shared video plan.
        width, height = plan.canvas
        rgb_path = tmp / "video.rgb"
        black = plan.black_rgb()
        n_frames = plan.frame_count
        with open(rgb_path, "wb") as vf:
            for n in range(n_frames):
                if cancelled():
                    raise RuntimeError("cancelled")
                png, rec = plan.frame_png(n)  # also applies end-anchor clamping
                if rec.reason != "ok":
                    vf.write(black)
                else:
                    from PIL import Image
                    import io

                    img = Image.open(io.BytesIO(png)).convert("RGB")
                    vf.write(img.tobytes())
                if n % 15 == 0:
                    job.progress = 0.05 + 0.55 * (n / max(n_frames, 1))

        # 2) Audio: exact mapping, float32 interleaved at 48 kHz.
        job.message = "rendering audio"

        def audio_progress(p: float) -> None:
            job.progress = 0.6 + 0.25 * p

        interleaved = plan.render_audio(progress=audio_progress, cancel=cancelled)
        aud_path = tmp / "audio.f32le"
        interleaved.astype("<f4").tofile(aud_path)
        channels = plan.audio_channels

        # 3) Encode.
        job.message = "encoding MP4"
        job.progress = 0.86
        out_mp4 = tmp / "out.mp4"
        cmd = [
            media.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pixel_format", "rgb24",
            "-video_size", f"{width}x{height}",
            "-framerate", str(OUTPUT_FPS),
            "-i", str(rgb_path),
            "-f", "f32le", "-ar", str(OUTPUT_SAMPLE_RATE),
            "-ac", str(channels), "-i", str(aud_path),
            "-frames:v", str(n_frames),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
            "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(out_mp4),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise media.FFmpegError(
                f"ffmpeg encode failed\n{proc.stderr[-2000:]}"
            )
        if cancelled():
            raise RuntimeError("cancelled")

        # 4) Mapping report + bound snapshot.
        report = plan.mapping_report()
        (tmp / "out.mapping.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (tmp / "out.snapshot.json").write_text(
            snapshot_pretty(snap), encoding="utf-8"
        )
        job.progress = 0.98
        job.message = "publishing"

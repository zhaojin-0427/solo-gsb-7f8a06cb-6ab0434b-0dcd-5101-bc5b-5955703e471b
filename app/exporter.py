"""Export worker: snapshot-bound, crash/cancel-safe publishing.

Lifecycle states: running -> exactly one of {done, failed, canceled}.
The finished MP4 and mapping JSON live in a private temp dir until the
render AND mux fully succeed; only then are they atomically renamed into
the published dir and the DB row claimed as 'done'.  A cancel request sets
a flag and tries to claim the 'canceled' terminal state first, so a
finish-vs-cancel race has a single winner and no half file is ever exposed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from fractions import Fraction
from pathlib import Path

from . import db as db_mod
from . import media as media_mod
from .render import (
    build_audio_records,
    build_video_records,
    render_audio_samples,
    render_video_frame,
    write_mapping_json,
)
from .time import FPS, SAMPLE_RATE, frac_str
from .timeline import build_edit_state


class CancelToken:
    def __init__(self):
        self.canceled = False

    def raise_if_canceled(self):
        if self.canceled:
            raise ExportCanceled()


class ExportCanceled(Exception):
    pass


def _snapshot_state(snapshot: dict):
    assets = {a["id"]: a for a in snapshot["assets"]}
    state = build_edit_state(snapshot["edit"], assets)
    return state, assets


def _render_to_files(state, tmp_dir: Path, token: CancelToken):
    """Produce exact-length raw video and audio intermediates."""
    nframes = int(state.duration * FPS)
    nsamples = int(state.duration * SAMPLE_RATE)
    w, h = state.width, state.height
    video_path = tmp_dir / "video.rgb"
    audio_path = tmp_dir / "audio.s16"
    decoders: dict = {}

    with video_path.open("wb") as vf:
        for i in range(nframes):
            if i % 30 == 0:
                token.raise_if_canceled()
            rgb, _ = render_video_frame(state, i, decoders)
            vf.write(rgb)

    token.raise_if_canceled()
    channels = 1
    if state.audio_asset_id is not None:
        row = state.assets[state.audio_asset_id]
        channels = int(row["channels"])

    chunk = 48000  # 1 s
    with audio_path.open("wb") as af:
        for s in range(0, nsamples, chunk):
            token.raise_if_canceled()
            c = min(chunk, nsamples - s)
            pcm, _ = render_audio_samples(state, s, c, decoders)
            af.write(pcm)

    return video_path, audio_path, channels


def _mux(video_path: Path, audio_path: Path, out_path: Path,
         w: int, h: int, channels: int, tmp_dir: Path) -> None:
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{w}x{h}", "-r", "30",
        "-i", str(video_path),
        "-f", "s16le", "-ar", "48000", "-ac", str(channels),
        "-i", str(audio_path),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg mux failed: {p.stderr[:800]}")


def run_export(db_path: str, job_id: str, exports_dir: str | Path,
               tmp_root: str | Path) -> None:
    conn = db_mod.connect(db_path)
    exports_dir = Path(exports_dir)
    tmp_root = Path(tmp_root)
    exports_dir.mkdir(parents=True, exist_ok=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    job = db_mod.get_export(conn, job_id)
    if job is None:
        return
    snapshot = job["snapshot_obj"]
    tmp_dir = tmp_root / job_id
    tmp_mp4 = tmp_dir / "out.mp4"
    tmp_map = tmp_dir / "mapping.json"

    def fail(err: str):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        db_mod.claim_terminal(conn, job_id, "failed", error=err)

    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        token = _tokens.register(job_id)
        state, _ = _snapshot_state(snapshot)

        vpath, apath, channels = _render_to_files(state, tmp_dir, token)
        _mux(vpath, apath, tmp_mp4, state.width, state.height, channels,
             tmp_dir)

        # Mapping records come from the same functions used by the render.
        video_recs = build_video_records(state)
        audio_recs = build_audio_records(state)
        write_mapping_json(tmp_map, state, video_recs, audio_recs,
                           {"edit": snapshot["edit"],
                            "assets": snapshot["assets"],
                            "captured_at": snapshot["captured_at"],
                            "nonce": snapshot["nonce"]})

        # Everything exists and is closed: publish atomically.
        final_mp4 = exports_dir / f"{job_id}.mp4"
        final_map = exports_dir / f"{job_id}.mapping.json"
        tmp_mp4.replace(final_mp4)
        tmp_map.replace(final_map)
        shutil.rmtree(tmp_dir, ignore_errors=True)

        if not db_mod.claim_terminal(
                conn, job_id, "done",
                output_path=str(final_mp4), mapping_path=str(final_map)):
            # Cancel won the race. Withdraw the publication; never leave
            # a half-state visible.
            final_mp4.unlink(missing_ok=True)
            final_map.unlink(missing_ok=True)
    except ExportCanceled:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        db_mod.claim_terminal(conn, job_id, "canceled")
    except Exception as e:  # noqa: BLE001 - recorded as job failure
        fail(f"{type(e).__name__}: {e}")
    finally:
        _tokens.discard(job_id)
        conn.close()


class _TokenRegistry:
    def __init__(self):
        self._tokens: dict[str, CancelToken] = {}
        self._lock = threading.Lock()

    def register(self, job_id: str) -> CancelToken:
        with self._lock:
            t = CancelToken()
            self._tokens[job_id] = t
            return t

    def request_cancel(self, job_id: str) -> bool:
        with self._lock:
            t = self._tokens.get(job_id)
            if t is not None:
                t.canceled = True
                return True
            return False

    def discard(self, job_id: str):
        with self._lock:
            self._tokens.pop(job_id, None)


_tokens = _TokenRegistry()


def request_cancel(job_id: str) -> bool:
    """Signal a running render to stop. Returns False if not active."""
    return _tokens.request_cancel(job_id)

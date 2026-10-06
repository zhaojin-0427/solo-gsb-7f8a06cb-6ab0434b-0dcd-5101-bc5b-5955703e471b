"""FFmpeg / FFprobe wrappers: probe, extract PNG frames, decode WAV PCM."""
from __future__ import annotations

import json
import shutil
import struct
import subprocess
import wave
from fractions import Fraction
from pathlib import Path
from typing import Optional

import numpy as np


class FFmpegError(RuntimeError):
    pass


def ffmpeg_bin() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise FFmpegError("ffmpeg not found on PATH")
    return exe


def ffprobe_bin() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise FFmpegError("ffprobe not found on PATH")
    return exe


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        head = " ".join(cmd[:3])
        tail = proc.stderr[-2000:]
        raise FFmpegError(
            f"command failed ({proc.returncode}): {head}...\n{tail}"
        )
    return proc


def probe_video(path: Path) -> dict:
    proc = _run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
    )
    data = json.loads(proc.stdout)
    stream = data["streams"][0]
    num, den = stream["r_frame_rate"].split("/")
    fps = Fraction(int(num), int(den))
    width = int(stream["width"])
    height = int(stream["height"])
    # Trust the displayed frame count when present; cross-check after extract.
    nb = stream.get("nb_frames")
    return {
        "fps": fps,
        "width": width,
        "height": height,
        "declared_frames": int(nb) if nb and nb != "N/A" else None,
        "duration": float(data["format"].get("duration", 0.0)),
    }


def probe_audio(path: Path) -> dict:
    """Read a PCM WAV header (uploads are required to be WAV)."""
    with wave.open(str(path), "rb") as wf:
        return {
            "sample_rate": wf.getframerate(),
            "channels": wf.getnchannels(),
            "samples": wf.getnframes(),
            "sampwidth": wf.getsampwidth(),
        }


def extract_frames(
    src: Path, out_dir: Path, fps: Fraction, canvas: tuple[int, int]
) -> int:
    """Extract every source frame as ``%08d.png`` (1-based), scaled to canvas.

    Frames are letterboxed (padded black) onto the common canvas so that no
    pixels from one camera ever fill the hole meant to come from another
    camera — padding is just per-frame geometry.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    w, h = canvas
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        "setsar=1"
    )
    pattern = str(out_dir / "%08d.png")
    _run(
        [
            ffmpeg_bin(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-vf",
            vf,
            "-vsync",
            "passthrough",
            "-start_number",
            "1",
            pattern,
        ]
    )
    count = len(list(out_dir.glob("*.png")))
    if count == 0:
        raise FFmpegError(f"no frames extracted from {src}")
    return count


def frame_path(frames_dir: Path, zero_based_index: int) -> Path:
    """File for display frame ``n`` (frame 0 was written as 00000001.png)."""
    return frames_dir / f"{zero_based_index + 1:08d}.png"


def decode_wav(path: Path, pcm_path: Path) -> tuple[np.ndarray, int, int, int]:
    """Decode the WAV to interleaved float32 PCM (native rate/channels)."""
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        n = wf.getnframes()
        raw = wf.readframes(n)

    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(width)
    if dtype is None:
        raise FFmpegError(f"unsupported WAV sample width: {width}")
    data = np.frombuffer(raw, dtype=dtype)
    if dtype == np.uint8:
        data = data.astype(np.float32) - 128.0
        data /= 128.0
    elif dtype == np.int16:
        data = data.astype(np.float32) / 32768.0
    else:
        data = data.astype(np.float32) / 2147483648.0
    data = np.asarray(data, dtype=np.float32)
    pcm_path.parent.mkdir(parents=True, exist_ok=True)
    data.tofile(pcm_path)
    return data, rate, channels, n

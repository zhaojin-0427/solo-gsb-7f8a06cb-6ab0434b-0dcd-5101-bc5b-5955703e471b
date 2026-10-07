"""FFmpeg-backed media probing and exact sample/frame access.

Decoders keep the raw asset in place and decode on demand; a render reads
the SAME frame/sample lookup the preview does.  No "freeze last frame"
behaviour exists anywhere: callers ask for a specific index and out-of-range
indices are reported as holes.
"""
from __future__ import annotations

import json
import subprocess
import threading
from fractions import Fraction
from pathlib import Path

from .time import SAMPLE_RATE


class MediaError(RuntimeError):
    pass


def _ffmpeg_path() -> str:
    return "ffmpeg"


def ffprobe_json(path: str | Path) -> dict:
    p = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        raise MediaError(f"ffprobe failed: {p.stderr.strip()}")
    return json.loads(p.stdout)


def parse_rate(s: str) -> Fraction:
    num, den = s.split("/")
    return Fraction(int(num), int(den))


def probe_video(path: str | Path) -> dict:
    info = ffprobe_json(path)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if v is None:
        raise MediaError("no video stream")
    # Prefer r_frame_rate (exact CFR tbn); sanity-check it against
    # avg_frame_rate to reject VFR inputs.
    rfr = parse_rate(v["r_frame_rate"])
    try:
        afr = parse_rate(v.get("avg_frame_rate", "0/1"))
    except (ValueError, ZeroDivisionError):
        afr = rfr
    fps = rfr if afr == 0 or abs(float(rfr - afr)) < 1e-6 else afr
    duration = Fraction(v.get("duration") or info["format"]["duration"])
    nb = v.get("nb_frames")
    nframes = int(nb) if nb and nb.isdigit() else int(duration * fps)
    return {
        "width": int(v["width"]),
        "height": int(v["height"]),
        "fps": fps,
        "duration": duration,
        "nframes": nframes,
        "pix_fmt": v.get("pix_fmt", ""),
    }


def probe_audio(path: str | Path) -> dict:
    info = ffprobe_json(path)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if a is None:
        raise MediaError("no audio stream")
    duration = Fraction(a.get("duration") or info["format"]["duration"])
    in_rate = int(a.get("sample_rate", 48000))
    channels = int(a.get("channels", 1))
    return {
        "in_sample_rate": in_rate,
        "channels": channels,
        "duration": duration,
        "nsamples": int(duration * in_rate),
    }


def _run_ffmpeg(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


class SourceVideo:
    """Lazily decodes ALL frames to raw RGB24 (samples are small, local tool)."""

    def __init__(self, path: str | Path, fps: Fraction, nframes: int,
                 width: int, height: int):
        self.path = str(path)
        self.fps = fps            # wall (container) fps, rational
        self.nframes = nframes
        self.width = width
        self.height = height
        self._frames: list[bytes] | None = None
        self._lock = threading.Lock()

    @property
    def frame_size(self) -> int:
        return self.width * self.height * 3

    def _decode(self) -> list[bytes]:
        with self._lock:
            if self._frames is not None:
                return self._frames
            proc = _run_ffmpeg([
                "-i", self.path,
                "-vsync", "0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
            ])
            raw = proc.stdout.read()
            err = proc.stderr.read()
            rc = proc.wait()
            if rc != 0:
                raise MediaError(f"video decode failed: {err.decode(errors='replace')[:500]}")
            fs = self.frame_size
            n = len(raw) // fs
            frames = [raw[i * fs:(i + 1) * fs] for i in range(n)]
            self._frames = frames
            self.nframes = n
            return frames

    def get_frame(self, wall_index: int) -> bytes | None:
        """Frame by wall-clock index [0, nframes); None = hole."""
        if wall_index < 0 or wall_index >= self.nframes:
            return None
        return self._decode()[wall_index]


class SourceAudio:
    """Decodes the whole track to interleaved s16le PCM at 48 kHz.

    Indices everywhere are FRAME indices (one frame = ``channels``
    interleaved 16-bit samples), counted from file wall-time zero.
    """

    def __init__(self, path: str | Path, channels: int, clock: Fraction,
                 material0: Fraction, material_span: Fraction):
        self.path = str(path)
        self.channels = channels
        self.clock = clock              # material sec per wall sec
        self.material0 = material0      # material timestamp of input frame 0
        self.material_span = material_span  # material-time length of the file
        self._pcm: bytes | None = None
        self._nframes = 0
        self._lock = threading.Lock()

    @property
    def frame_bytes(self) -> int:
        return self.channels * 2

    def _decode(self) -> bytes:
        with self._lock:
            if self._pcm is not None:
                return self._pcm
            try:
                proc = _run_ffmpeg([
                    "-i", self.path,
                    "-vn",
                    "-af", f"aresample={int(SAMPLE_RATE)}:resampler=soxr",
                    "-f", "s16le", "-acodec", "pcm_s16le", "-ac",
                    str(self.channels), "-",
                ])
                raw = proc.stdout.read()
                err = proc.stderr.read()
                rc = proc.wait()
            except Exception:
                rc, raw, err = 1, b"", b""
            if rc != 0:
                # soxr may be unavailable in minimal builds; retry default.
                proc = _run_ffmpeg([
                    "-i", self.path, "-vn",
                    "-ar", str(int(SAMPLE_RATE)),
                    "-f", "s16le", "-acodec", "pcm_s16le",
                    "-ac", str(self.channels), "-",
                ])
                raw = proc.stdout.read()
                err2 = proc.stderr.read()
                rc = proc.wait()
                if rc != 0:
                    raise MediaError(
                        f"audio decode failed: {err.decode(errors='replace')[:300]} "
                        f"{err2.decode(errors='replace')[:300]}"
                    )
            self._pcm = raw
            self._nframes = len(raw) // self.frame_bytes
            return raw

    @property
    def nframes(self) -> int:
        if self._pcm is None:
            self._decode()
        return self._nframes

    def frame_index(self, u: Fraction) -> int:
        """Material time u -> interleaved frame index in the decoded PCM.

        displayed(wall_t) = material0 + clock*wall_t
        => wall_t = (u - material0)/clock.
        """
        wall_t = (u - self.material0) / self.clock
        return wall_t * SAMPLE_RATE


# Process-wide decode cache: asset_id+path identity.
_video_cache: dict[str, SourceVideo] = {}
_audio_cache: dict[str, SourceAudio] = {}
_cache_lock = threading.Lock()


def get_source_video(asset_id: int, path: str, fps: Fraction, nframes: int,
                     width: int, height: int) -> SourceVideo:
    key = f"v:{asset_id}:{path}"
    with _cache_lock:
        sv = _video_cache.get(key)
        if sv is None:
            sv = SourceVideo(path, fps, nframes, width, height)
            _video_cache[key] = sv
        return sv


def get_source_audio(asset_id: int, path: str, channels: int, clock: Fraction,
                     material0: Fraction, material_span: Fraction) -> SourceAudio:
    key = f"a:{asset_id}:{path}"
    with _cache_lock:
        sa = _audio_cache.get(key)
        if sa is None:
            sa = SourceAudio(path, channels, clock, material0, material_span)
            _audio_cache[key] = sa
        return sa

"""Rendering shared by preview and export.

A :class:`Plan` is built from an immutable snapshot (see :mod:`app.state`)
and is the *only* thing that turns target times into source locations.  PNG
frame preview, the WAV preview and the MP4 export therefore always agree.
"""
from __future__ import annotations

import io
import math
import wave
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image

from .mapping import (
    Anchor,
    AudioBlock,
    Cut,
    FrameRecord,
    Piecewise,
    plan_audio,
    plan_video,
)
from .rationals import OUTPUT_FPS, OUTPUT_SAMPLE_RATE, parse_q, q_to_json
from . import media


@dataclass
class PlanAsset:
    kind: str
    path: Path
    fps: Optional[Fraction]
    frames_dir: Optional[Path]
    frame_count: Optional[int]
    width: Optional[int]
    height: Optional[int]
    sample_rate: Optional[int]
    channels: Optional[int]
    samples: Optional[int]
    pcm_path: Optional[Path]


class Plan:
    def __init__(self, snap: dict):
        self.duration = parse_q(snap["duration"], "duration")
        self.canvas = tuple(snap["canvas"])
        self.assets = {
            role: PlanAsset(
                kind=a["kind"],
                path=Path(a["path"]),
                fps=parse_q(a["fps"], "fps") if a.get("fps") else None,
                frames_dir=Path(a["frames_dir"]) if a.get("frames_dir") else None,
                frame_count=a.get("frame_count"),
                width=a.get("width"),
                height=a.get("height"),
                sample_rate=a.get("sample_rate"),
                channels=a.get("channels"),
                samples=a.get("samples"),
                pcm_path=Path(a["pcm_path"]) if a.get("pcm_path") else None,
            )
            for role, a in snap["assets"].items()
        }
        self.pieces = {
            role: Piecewise(
                [Anchor(parse_q(m, "m"), parse_q(o, "o")) for m, o in pairs]
            )
            for role, pairs in snap["anchors"].items()
            if len(pairs) >= 2
        }
        self.cuts = sorted(
            (Cut(parse_q(s, "s"), parse_q(e, "e"), cam)
             for s, e, cam in snap["cuts"]),
            key=lambda c: c.start,
        )
        self._video: Optional[list[FrameRecord]] = None
        self._audio: Optional[list[AudioBlock]] = None
        self._pcm: dict[str, np.ndarray] = {}

    @property
    def video_plan(self) -> list[FrameRecord]:
        if self._video is None:
            self._video = plan_video(
                self.duration, self.cuts, self.pieces,
                {r: a.fps for r, a in self.assets.items()
                 if a.kind == "video" and a.fps is not None},
            )
        return self._video

    @property
    def audio_plan(self) -> list[AudioBlock]:
        if self._audio is None:
            self._audio = plan_audio(
                self.duration, self.pieces["master"], OUTPUT_SAMPLE_RATE
            )
        return self._audio

    @property
    def frame_count(self) -> int:
        return len(self.video_plan)

    @property
    def audio_channels(self) -> int:
        return int(self.assets["master"].channels or 1)

    def frame_record(self, n: int) -> Optional[FrameRecord]:
        vp = self.video_plan
        if 0 <= n < len(vp):
            return vp[n]
        return None

    # -- video -----------------------------------------------------------

    def _black_png(self) -> bytes:
        w, h = self.canvas
        img = Image.new("RGB", (w, h), (0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def frame_png(self, n: int) -> tuple[bytes, FrameRecord]:
        rec = self.video_plan[n]
        if rec.reason != "ok":
            return self._black_png(), rec
        asset = self.assets[rec.camera]
        sf = rec.source_frame
        if sf == asset.frame_count:
            # Target instant exactly on the material's final anchor maps to
            # the last existing display frame (the open end of its interval).
            sf -= 1
        elif sf > asset.frame_count or sf < 0:
            return self._black_png(), rec
        p = media.frame_path(asset.frames_dir, sf)
        return p.read_bytes(), rec

    def black_rgb(self) -> bytes:
        w, h = self.canvas
        return np.zeros((h, w, 3), dtype=np.uint8).tobytes()

    # -- audio -----------------------------------------------------------

    def _master_pcm(self) -> np.ndarray:
        if "master" not in self._pcm:
            asset = self.assets["master"]
            data = np.fromfile(asset.pcm_path, dtype=np.float32)
            self._pcm["master"] = data.reshape(-1, asset.channels)
        return self._pcm["master"]

    def render_audio(
        self,
        progress: Optional[Callable[[float], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> np.ndarray:
        """Return interleaved float32 output samples at 48 kHz.

        Per-sample source positions come from exact integer arithmetic derived
        from the rational affine maps; only the linear interpolation weight is
        floating point.  Silence blocks are written as zeros — never held.
        """
        src = self._master_pcm()
        n_src, channels = src.shape
        src_rate = int(self.assets["master"].sample_rate)
        rate = OUTPUT_SAMPLE_RATE
        blocks = self.audio_plan
        n_out = blocks[-1].sample_end if blocks else 0
        out = np.zeros((n_out, channels), dtype=np.float32)
        master = self.pieces["master"]
        anchor_out = [a.output for a in master.anchors]

        for bi, blk in enumerate(blocks):
            if cancel and cancel():
                raise RuntimeError("cancelled")
            if blk.silence:
                continue
            # Find the constant affine segment a,b covering this block.
            t_mid = (Fraction(blk.sample_start + blk.sample_end, 2)
                     + Fraction(1, 2)) / rate
            import bisect
            seg = min(bisect.bisect_right(anchor_out, t_mid) - 1,
                      len(master.anchors) - 2)
            a = master._a[seg]
            b = master._b[seg]
            s0, s1 = blk.sample_start, blk.sample_end
            den = 2 * rate * a.denominator * b.denominator
            term2 = int(b.numerator) * (2 * rate * int(a.denominator))
            # Largest |numerator| before multiplying by the source rate; keep
            # the vector in int64 unless the rational constants are pathological.
            max_abs = (abs(int(a.numerator)) * (2 * max(s0, s1 - 1) + 1)
                       * int(b.denominator) + abs(term2)) * int(src_rate)
            use_64 = max_abs < (1 << 62)
            dtype = np.int64 if use_64 else object
            s = np.arange(s0, s1, dtype=np.int64)
            num = (
                int(a.numerator) * (2 * s + 1) * int(b.denominator) + term2
            ).astype(object if not use_64 else np.int64, copy=False)
            scaled = num * int(src_rate)
            if use_64:
                idx = scaled // int(den)
                frac = ((scaled - idx * int(den)) / int(den)).astype(np.float32)
            else:  # exact big-integer path
                idx = np.array(
                    [int(x) // den for x in scaled], dtype=np.int64
                )
                frac = np.array(
                    [(int(x) - int(ix) * den) / den
                     for x, ix in zip(scaled, idx)],
                    dtype=np.float32,
                )

            valid = (idx >= 0) & (idx < n_src)
            idx_c = np.clip(idx, 0, n_src - 1)
            nxt_c = np.clip(idx + 1, 0, n_src - 1)
            v0 = src[idx_c]
            v1 = src[nxt_c]
            vals = v0 + (v1 - v0) * frac[:, None]
            vals[~valid] = 0.0
            out[s0:s1] = vals
            if progress:
                progress((bi + 1) / max(len(blocks), 1))
        return np.ascontiguousarray(out.reshape(-1))

    def render_wav(self) -> bytes:
        interleaved = self.render_audio()
        pcm16 = np.clip(interleaved, -1.0, 1.0)
        pcm16 = (pcm16 * 32767.0).astype("<i2")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(self.audio_channels)
            wf.setsampwidth(2)
            wf.setframerate(OUTPUT_SAMPLE_RATE)
            wf.writeframes(pcm16.tobytes())
        return buf.getvalue()

    # -- mapping report --------------------------------------------------

    def mapping_report(self) -> dict:
        frames = [
            {
                "frame": r.index,
                "output_time": q_to_json(r.output_time),
                "output_time_seconds": _float(r.output_time),
                "camera": r.camera,
                "material_time": q_to_json(r.material_time) if r.material_time is not None else None,
                "source_fps": q_to_json(r.source_fps) if r.source_fps is not None else None,
                "source_frame": r.source_frame,
                "fill": r.reason if r.reason != "ok" else None,
            }
            for r in self.video_plan
        ]
        audio = [
            {
                "sample_start": b.sample_start,
                "sample_end": b.sample_end,
                "output_time_start": q_to_json(b.output_start),
                "output_time_end": q_to_json(b.output_end),
                "material_time_start": q_to_json(b.material_start) if b.material_start is not None else None,
                "material_time_end": q_to_json(b.material_end) if b.material_end is not None else None,
                "silence": b.silence,
                "fill": b.reason if b.reason != "ok" else None,
            }
            for b in self.audio_plan
        ]
        return {
            "output_fps": q_to_json(OUTPUT_FPS),
            "output_sample_rate": OUTPUT_SAMPLE_RATE,
            "canvas": list(self.canvas),
            "duration": q_to_json(self.duration),
            "video_frames": frames,
            "audio_blocks": audio,
        }


def _float(q: Fraction) -> float:
    return float(q)

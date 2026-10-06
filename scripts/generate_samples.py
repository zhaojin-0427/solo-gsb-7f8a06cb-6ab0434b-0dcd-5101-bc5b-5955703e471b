#!/usr/bin/env python3
"""Generate two camera clips with burned-in display clocks and a master WAV.

The two videos deliberately differ in frame rate, start offset and *clock
rate* (the wall time shown on screen advances at a non-1.0 ratio of real
time), which is exactly what display-timestamp anchors are for:

  camera A: 30 fps, 12 s long, display starts 00:00.000, clock rate 1.0
  camera B: 24 fps, 10 s long, display starts 00:01.500, clock rate 0.8
            (one display second lasts 1.25 real seconds)
  master WAV: 44.1 kHz mono, short 1 kHz pulses at each integer master second,
              10 s total

Every video frame is rendered from lossless per-frame PNGs fed to FFmpeg,
and the burned number is the *display frame index* (matching the displayed
clock), so the editor's ``source_frame`` can be verified pixel-wise.

Usage: python scripts/generate_samples.py --out samples
"""
from __future__ import annotations

import argparse
import math
import struct
import subprocess
import tempfile
import wave
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw

WIDTH, HEIGHT = 480, 270
BG = (18, 18, 24)


def ffmpeg_bin() -> str:
    import shutil

    exe = shutil.which("ffmpeg")
    if not exe:
        raise SystemExit("ffmpeg not found on PATH")
    return exe


def fmt_clock(t_seconds: float) -> str:
    if t_seconds < 0:
        neg = "-"
        t_seconds = -t_seconds
    else:
        neg = ""
    m = int(t_seconds // 60)
    s = t_seconds - m * 60
    return f"{neg}{m:02d}:{s:06.3f}"


def render_frame(path: Path, label: str, frame_no: int, disp_time: float,
                 color: tuple[int, int, int]) -> None:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    d = ImageDraw.Draw(img)
    # Big frame number, centered.
    fnum = f"FRAME {frame_no:04d}"
    d.text((20, 70), fnum, fill=color)
    d.text((20, 110), label, fill=(200, 200, 210))
    d.rectangle([0, HEIGHT - 58, WIDTH, HEIGHT], fill=(8, 8, 12))
    d.text((18, HEIGHT - 50), f"DISPLAY {fmt_clock(disp_time)}", fill=(255, 255, 255))
    # Corner ruler so frame identity is unambiguous even at small sizes.
    d.rectangle([0, 0, 6 + (frame_no % 40) * 4, 6], fill=color)
    img.save(path, format="PNG")


def make_video(out_path: Path, *, label: str, fps: Fraction, duration_s: float,
               display_start: float, clock_rate: float,
               color: tuple[int, int, int]) -> int:
    n = int(round(duration_s * float(fps)))
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        for k in range(n):
            # Real elapsed time at the frame center; displayed clock follows.
            real_t = (k + 0.5) / float(fps)
            disp = display_start + clock_rate * real_t
            render_frame(tdir / f"{k + 1:08d}.png", label, k, disp, color)
        cmd = [
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-framerate", f"{fps.numerator}/{fps.denominator}",
            "-i", str(tdir / "%08d.png"),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-crf", "16", "-g", str(int(fps)),
            str(out_path),
        ]
        subprocess.run(cmd, check=True)
    return n


def make_wav(out_path: Path, *, rate: int = 44100, duration_s: float = 10.0,
             pulse_hz: int = 1000, pulse_ms: int = 50) -> None:
    n = int(rate * duration_s)
    samples = [0.0] * n
    plen = int(rate * pulse_ms / 1000)
    for sec in range(int(duration_s) + 1):
        start = sec * rate
        for i in range(plen):
            if start + i < n:
                env = math.sin(math.pi * i / plen)  # fade, avoids clicks
                samples[start + i] = 0.8 * env * math.sin(
                    2 * math.pi * pulse_hz * i / rate
                )
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", max(-32767, min(32767, int(round(s * 32767)))))
            for s in samples
        )
        wf.writeframes(frames)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="samples", help="output directory")
    ap.add_argument("--a-fps", default="30")
    ap.add_argument("--b-fps", default="24")
    ap.add_argument("--b-start", type=float, default=1.5,
                    help="camera B display start offset (seconds)")
    ap.add_argument("--b-rate", type=float, default=0.8,
                    help="camera B displayed-clock rate vs real time")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    na = make_video(
        out / "cameraA.mp4", label="CAM A", fps=Fraction(args.a_fps),
        duration_s=12.0, display_start=0.0, clock_rate=1.0, color=(120, 170, 255),
    )
    nb = make_video(
        out / "cameraB.mp4", label="CAM B", fps=Fraction(args.b_fps),
        duration_s=10.0, display_start=args.b_start,
        clock_rate=args.b_rate, color=(120, 230, 180),
    )
    make_wav(out / "master.wav")
    print(f"wrote {out/'cameraA.mp4'} ({na} frames @ {args.a_fps} fps)")
    print(f"wrote {out/'cameraB.mp4'} ({nb} frames @ {args.b_fps} fps, "
          f"display start {args.b_start}s, clock rate {args.b_rate})")
    print(f"wrote {out/'master.wav'} (44.1 kHz, 1 Hz pulses, 10 s)")
    print()
    print("Suggested verification anchors (display frame k @ fps F:")
    print("material time = (k + 0.5)/F):")
    print("  A: (1/60, 0), (11 + 11/12, 8 + 23/24)")
    print("  B: (1/48, 5/24),     (7 + 47/48, 8 + 23/24)")
    print("  master: (1/88200, 0), (793801/88200, 215/24)")


if __name__ == "__main__":
    main()

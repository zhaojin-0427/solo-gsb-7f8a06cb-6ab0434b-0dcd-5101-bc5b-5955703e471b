#!/usr/bin/env python3
"""Generate two camera videos + one WAV master with DIFFERENT start
timestamps, frame rates and clock speeds, for verifying sync.

Video frame layout (RGB, 320x240):
  * top 16px: 16-bit barcode of the WALL frame index (white=1), MSB first,
    readable back from decoded frames by check_sample.py
  * middle: human text "cam A frame 000123 wall 004.100000 mat -0.500000"
    (wall seconds on the container clock, mat seconds on the displayed
     timestamp clock: mat = material0 + clock*wall)
  * whole frame is a deterministic shade so black/hole frames are obvious
Audio (WAV): one second starts with a 20 ms 1 kHz sine "blip" (both
channels); the rest of the second is silence.

Default camera specs deliberately differ:
  cam A: 30/1.001 fps (~29.97), material0 = -1/2 s, clock = 1
  cam B: 24 fps,              material0 =  1/4 s, clock = 9/10

Usage:
  python scripts/sample_generator.py [out_dir]
"""
from __future__ import annotations

import math
import struct
import subprocess
import sys
import wave
from fractions import Fraction
from pathlib import Path

W, H = 320, 240
SR = 48000

# Minimal 5x7 bitmap font for the characters we need.
FONT = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "a": ["00000", "00000", "01110", "00001", "01111", "10001", "01111"],
    "b": ["10000", "10000", "11110", "10001", "10001", "10001", "11110"],
    "c": ["00000", "00000", "01111", "10000", "10000", "10000", "01111"],
    "f": ["00111", "01000", "10000", "11110", "10000", "10000", "10000"],
    "r": ["00000", "00000", "10110", "11001", "10000", "10000", "10000"],
    "m": ["00000", "00000", "11010", "10101", "10101", "10101", "10101"],
    "e": ["00000", "00000", "01110", "10001", "11111", "10000", "01110"],
    "w": ["00000", "00000", "10101", "10101", "10101", "11011", "01010"],
    "l": ["01100", "00100", "00100", "00100", "00100", "00101", "01110"],
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "00110", "00110"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    ":": ["00000", "00110", "00110", "00000", "00110", "00110", "00000"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
}


def draw_text(buf: bytearray, text: str, x0: int, y0: int,
              color=(235, 235, 235), scale: int = 2):
    x = x0
    for ch in text:
        glyph = FONT.get(ch, FONT[" "])
        for ry, rowbits in enumerate(glyph):
            for rx, b in enumerate(rowbits):
                if b == "1":
                    for sy in range(scale):
                        for sx in range(scale):
                            px = x + rx * scale + sx
                            py = y0 + ry * scale + sy
                            if 0 <= px < W and 0 <= py < H:
                                o = (py * W + px) * 3
                                buf[o], buf[o + 1], buf[o + 2] = color
        x += 6 * scale


def make_frame(cam_letter: str, index: int, wall_t: Fraction,
               material0: Fraction, clock: Fraction) -> bytes:
    # background: subtly different shade per camera, modulated by index
    base = 26 + (index % 5) * 4
    buf = bytearray((base, base + 6, base + 14))
    buf *= W * H
    # 16-bit barcode band, rows 0..15: the reader assembles blocks
    # MSB-first, so block 0 is bit 15 and block 15 is bit 0.
    for bit in range(16):
        on = bool(index & (1 << (15 - bit)))
        if on:
            x0, x1 = bit * 20, bit * 20 + 18
            for y in range(16):
                row = y * W * 3
                for x in range(x0, x1):
                    oo = row + x * 3
                    buf[oo] = buf[oo + 1] = buf[oo + 2] = 255
    mat = material0 + clock * wall_t
    label = (f"{cam_letter} f{index:06d} "
             f"wall {float(wall_t):9.5f} mat {float(mat):9.5f}")
    draw_text(buf, label, 8, 40, scale=2)
    # extra band separating text lines
    draw_text(buf, f"cam{cam_letter} {float(material0):.3f}s "
                   f"clock {clock.numerator}/{clock.denominator}",
              8, 70, color=(180, 220, 255), scale=2)
    # colored stripe per camera so switches are visible
    stripe = (200, 60, 60) if cam_letter == "a" else (60, 160, 220)
    for y in range(H - 24, H):
        o = (y * W) * 3
        for x in range(W):
            oo = o + x * 3
            buf[oo], buf[oo + 1], buf[oo + 2] = stripe
    return bytes(buf)


def gen_camera(path: Path, cam_letter: str, fps: Fraction,
               material0: Fraction, clock: Fraction, duration: float):
    nframes = int(duration * float(fps))
    vf = fps.numerator / fps.denominator
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{W}x{H}", "-r", f"{fps.numerator}/{fps.denominator}",
        "-i", "-",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-g", str(int(vf * 2)),
        "-r", f"{fps.numerator}/{fps.denominator}",
        str(path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for i in range(nframes):
            wall_t = Fraction(i, fps)
            proc.stdin.write(make_frame(cam_letter, i, wall_t,
                                        material0, clock))
    finally:
        proc.stdin.close()
    rc = proc.wait()
    if rc != 0:
        raise SystemExit(f"ffmpeg failed for {path}")
    return nframes


def gen_audio(path: Path, duration: float):
    n = int(duration * SR)
    frames = bytearray()
    amp = 12000
    blip_n = int(0.020 * SR)  # 20 ms
    for i in range(n):
        sec = i // SR
        within = i % SR
        if within < blip_n:
            env = min(1.0, within / 200, (blip_n - within) / 200)
            v = int(amp * env * math.sin(2 * math.pi * 1000 * i / SR))
        else:
            v = 0
        frames += struct.pack("<hh", v, v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(frames))


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
    ffmpeg = sys.argv[2] if len(sys.argv) > 2 else "ffmpeg"
    import os
    os.environ["PATH"] = os.path.dirname(ffmpeg) + os.pathsep + os.environ["PATH"] \
        if os.path.dirname(ffmpeg) else os.environ["PATH"]
    out.mkdir(parents=True, exist_ok=True)
    duration = 6.0

    cams = [
        ("cam_a.mp4", "a", Fraction(30000, 1001), Fraction(-1, 2), Fraction(1, 1)),
        ("cam_b.mp4", "b", Fraction(24, 1),        Fraction(1, 4),  Fraction(9, 10)),
    ]
    for name, letter, fps, m0, clock in cams:
        n = gen_camera(out / name, letter, fps, m0, clock, duration)
        print(f"wrote {out/name}: {n} frames @ {float(fps):.6f} fps, "
              f"material0={m0}, clock={clock}")
    gen_audio(out / "master.wav", duration)
    print(f"wrote {out/'master.wav'}: {duration}s, 48kHz stereo, "
          f"20ms 1kHz blip at each integer second")
    print("\nSuggested upload + clock settings:")
    print("  cam_a.mp4  material0=-1/2 clock=1   anchors (-1/2,0)->(11/2,4)")
    print("  cam_b.mp4  material0= 1/4 clock=9/10 anchors ( 1/4,0)->(113/20,4)")
    print("  master.wav material0=0    clock=1    anchors (0,0)->(4,4)")
    print("  (anchor end = material0 + clock * 6s wall duration; the checker")
    print("   re-derives exact values from probed metadata)")


if __name__ == "__main__":
    main()

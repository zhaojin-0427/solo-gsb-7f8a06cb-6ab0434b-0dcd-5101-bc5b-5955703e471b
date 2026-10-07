"""Minimal pure-Python RGB image helpers: nearest-neighbour scale + PNG.

No third-party imaging dependency needed; preview frames are small.
"""
from __future__ import annotations

import struct
import zlib

_BLACK_CACHE: dict[tuple[int, int], bytes] = {}


def black_frame(width: int, height: int) -> bytes:
    key = (width, height)
    buf = _BLACK_CACHE.get(key)
    if buf is None:
        buf = b"\x00" * (width * height * 3)
        _BLACK_CACHE[key] = buf
    return buf


def scale_rgb(src: bytes, sw: int, sh: int, dw: int, dh: int) -> bytes:
    """Nearest-neighbour resize, placed at (0,0) on a black canvas.

    Aspect ratio is preserved by letterbox centering.
    """
    out = bytearray(black_frame(dw, dh))
    # largest scale that fits
    scale = min(dw / sw, dh / sh)
    rw = max(1, round(sw * scale))
    rh = max(1, round(sh * scale))
    ox = (dw - rw) // 2
    oy = (dh - rh) // 2
    for y in range(rh):
        sy = min(sh - 1, int((y + 0.5) * sh / rh))
        row_dst = (oy + y) * dw + ox
        for x in range(rw):
            sx = min(sw - 1, int((x + 0.5) * sw / rw))
            si = (sy * sw + sx) * 3
            di = (row_dst + x) * 3
            out[di] = src[si]
            out[di + 1] = src[si + 1]
            out[di + 2] = src[si + 2]
    return bytes(out)


def fit_to_canvas(src: bytes, sw: int, sh: int, dw: int, dh: int) -> bytes:
    if (sw, sh) == (dw, dh):
        return src
    return scale_rgb(src, sw, sh, dw, dh)


def encode_png(rgb: bytes, width: int, height: int) -> bytes:
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)  # filter type 0 (None)
        raw += rgb[y * stride:(y + 1) * stride]
    comp = zlib.compress(bytes(raw), 6)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", comp) + chunk(b"IEND", b"")

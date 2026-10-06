"""Rational-number helpers.

All edit-time quantities (anchor points, cuts, duration, source times) are
carried as :class:`fractions.Fraction` end to end.  The JavaScript side and
JSON documents exchange fractions either as ``"num/den"`` strings or as
decimal strings; :func:`parse_q` accepts both.
"""
from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

OUTPUT_FPS = Fraction(30, 1)
OUTPUT_SAMPLE_RATE = 48000


def parse_q(value: Any, name: str = "value") -> Fraction:
    """Parse ``int`` / ``Fraction`` / ``"n/d"`` / decimal text into Fraction.

    Floats are routed through ``repr`` so that a JSON-origin ``0.1`` becomes
    exactly ``1/10`` instead of a binary approximation.
    """
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(repr(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{name} is empty")
        if "/" in text:
            num, _, den = text.partition("/")
            try:
                result = Fraction(int(num.strip()), int(den.strip()))
            except (ValueError, ZeroDivisionError) as exc:
                raise ValueError(f"{name} is not a valid fraction: {text!r}") from exc
            return result
        try:
            return Fraction(text)
        except ValueError:
            try:  # decimal float text, e.g. "0.1"
                return Fraction(float.hex(float(text)))
            except ValueError as exc:
                raise ValueError(f"{name} is not a valid number: {text!r}") from exc
    raise ValueError(f"{name} has unsupported type {type(value).__name__}")


def q_to_json(value: Fraction) -> str:
    """Stable JSON form: fraction when non-integral, else plain integer text."""
    value = Fraction(value)
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def frame_center(index: int, fps: Fraction = OUTPUT_FPS) -> Fraction:
    """Target time at the *center* of output frame ``index`` (half-open cell)."""
    return (index + Fraction(1, 2)) / fps


def frame_count(duration: Fraction, fps: Fraction = OUTPUT_FPS) -> int:
    """Number of half-open cells whose centers lie in ``[0, duration)``.

    Cell n is centered at (n + 1/2)/fps, so it is included iff
    n + 1/2 < duration*fps.  Counting from n = 0 gives
    ``ceil(duration*fps - 1/2)`` cells — e.g. ``75/8`` s at 30 fps is 281
    frames (frame 280's center 9.35 s is in, frame 281's 9.383 s is out),
    while an exact integer number of cells is never rounded up.
    The same formula governs audio samples, keeping the two media aligned.
    """
    import math

    return math.ceil(duration * fps - Fraction(1, 2))

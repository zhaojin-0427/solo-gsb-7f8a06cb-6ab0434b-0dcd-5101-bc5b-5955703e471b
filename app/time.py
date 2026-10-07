"""Exact rational time handling.

All times in the system are seconds represented as ``fractions.Fraction``.
Nothing is rounded to float or to whole frames/samples until the final
"which source frame / source sample covers this exact instant" lookup,
so segment durations are never rounded before being concatenated.
"""
from __future__ import annotations

from fractions import Fraction

FPS = Fraction(30)
SAMPLE_RATE = Fraction(48000)


def Q(value, name: str = "value", *, allow_negative: bool = False) -> Fraction:
    """Coerce an incoming JSON value to an exact Fraction.

    Accepts integers, floats (parsed via their shortest decimal repr,
    matching how a browser sends them), strings like ``"3/2"``,
    ``"-1/2"`` and ``"60/1.001"``.  Non-negative by default;
    ``allow_negative`` is used for displayed-timestamp origins.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    if isinstance(value, int):
        result = Fraction(value)
    elif isinstance(value, Fraction):
        result = value
    elif isinstance(value, float):
        # repr(float) is the shortest decimal that round-trips, so the
        # exact intended decimal value is preserved.
        result = Fraction(repr(value))
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            raise ValueError(f"{name} is empty")
        try:
            if "/" in s:
                num, den = s.split("/", 1)
                result = Fraction(int(num.strip()), int(den.strip()))
            else:
                result = Fraction(s)
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError(f"{name} is not a valid rational: {value!r}") from exc
    else:
        raise ValueError(f"{name} must be a number, got {type(value).__name__}")
    if not allow_negative and result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def frame_time(i: int) -> Fraction:
    """Target time (half-open frame start) of output frame ``i``."""
    return Fraction(i, FPS)


def sample_time(n: int) -> Fraction:
    """Target time of output sample frame ``n`` (sample start)."""
    return Fraction(n, SAMPLE_RATE)


def frac_str(f: Fraction) -> str:
    """Stable wire format for a Fraction: integer, or ``num/den``."""
    f = Fraction(f)
    if f.denominator == 1:
        return str(f.numerator)
    return f"{f.numerator}/{f.denominator}"


def frac_float(f: Fraction) -> float:
    return float(f)

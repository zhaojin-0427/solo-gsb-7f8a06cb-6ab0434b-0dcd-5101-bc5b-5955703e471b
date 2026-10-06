"""The single source of truth for the edit timeline mapping.

Every consumer (frame preview, WAV preview, MP4 export, mapping report)
resolves target times through the functions in this module, so they cannot
disagree.

Coordinates
----------
* *material time*: seconds on a clip's own displayed clock / the master WAV's
  own sample clock.
* *output time*: seconds on the finished timeline, starting at 0.

Anchors are strictly increasing ``(material, output)`` pairs, at least two
per material.  Between consecutive anchors the mapping is linear.  Outside
the anchor range the material has nothing to contribute (black frames /
silence) — the previous clip's last frame is never held.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Optional, Sequence

from .rationals import OUTPUT_FPS, frame_center, frame_count, parse_q


class ValidationError(ValueError):
    """Raised when an edit document is inconsistent."""


@dataclass(frozen=True)
class Anchor:
    material: Fraction
    output: Fraction


@dataclass(frozen=True)
class Cut:
    """Half-open camera selection ``[start, end)`` on the output timeline."""

    start: Fraction
    end: Fraction
    camera: str

    def contains(self, t: Fraction) -> bool:
        return self.start <= t < self.end


class Piecewise:
    """Segment-linear material-time -> output-time map.

    Lookup is provided in the *output -> material* direction
    (:meth:`material_at`), which is what renderers need.
    """

    def __init__(self, anchors: Sequence[Anchor]):
        if len(anchors) < 2:
            raise ValidationError("at least two anchors are required")
        mats = [a.material for a in anchors]
        outs = [a.output for a in anchors]
        for seq, label in ((mats, "material"), (outs, "output")):
            if any(b <= a for a, b in zip(seq, seq[1:])):
                raise ValidationError(f"anchor {label} times must be strictly increasing")
        self.anchors = tuple(anchors)
        self.mat_start = mats[0]
        self.mat_end = mats[-1]
        self.out_start = outs[0]
        self.out_end = outs[-1]
        # material = a * output + b, per segment
        self._a: list[Fraction] = []
        self._b: list[Fraction] = []
        for lo, hi in zip(anchors, anchors[1:]):
            a = (hi.material - lo.material) / (hi.output - lo.output)
            b = lo.material - a * lo.output
            self._a.append(a)
            self._b.append(b)

    def covers_output(self, t: Fraction) -> bool:
        # Both end anchors belong to the covered set; a target instant exactly
        # on the final anchor maps to that anchor's material time.
        return self.out_start <= t <= self.out_end

    def material_at(self, t: Fraction) -> Optional[Fraction]:
        """Map an output time to material time; ``None`` outside anchor range."""
        if not self.covers_output(t):
            return None
        outs = [a.output for a in self.anchors]
        # Rightmost anchor with output <= t (bisect_right - 1).
        import bisect

        i = bisect.bisect_right(outs, t) - 1
        if i >= len(self._a):  # t exactly equals the final anchor: last segment
            i = len(self._a) - 1
        return self._a[i] * t + self._b[i]


def validate_cuts(cuts: Sequence[Cut], duration: Fraction) -> None:
    if duration < 0:
        raise ValidationError("duration must be >= 0")
    prev_end = Fraction(0)
    for cut in sorted(cuts, key=lambda c: c.start):
        if cut.start < prev_end:
            raise ValidationError("camera intervals must not overlap")
        if cut.end <= cut.start:
            raise ValidationError("camera intervals must be half-open with end > start")
        if cut.start < 0 or cut.end > duration:
            raise ValidationError("camera interval lies outside the timeline")
        prev_end = cut.end


def cut_at(cuts: Sequence[Cut], t: Fraction) -> Optional[Cut]:
    for cut in cuts:  # tiny lists; kept sorted by callers
        if cut.contains(t):
            return cut
    return None


@dataclass(frozen=True)
class FrameRecord:
    """Where output frame ``index`` comes from."""

    index: int
    output_time: Fraction
    camera: Optional[str]          # None -> black fill
    material_time: Optional[Fraction]
    source_fps: Optional[Fraction]
    source_frame: Optional[int]    # covered display frame on that material
    reason: str                    # "ok" | "no-camera" | "outside-anchors"


@dataclass(frozen=True)
class AudioBlock:
    """Constant-affine output sample interval ``[sample_start, sample_end)``."""

    sample_start: int
    sample_end: int
    output_start: Fraction
    output_end: Fraction
    material_start: Optional[Fraction]
    material_end: Optional[Fraction]
    silence: bool
    reason: str                    # "ok" | "outside-anchors"


def source_frame_index(material_time: Fraction, source_fps: Fraction) -> int:
    """Display frame whose half-open presentation interval covers t (floor).

    Frame n covers ``[n/fps, (n+1)/fps)``.  A tiny non-rational drift cannot
    occur because ``material_time`` is itself an exact Fraction.
    """
    return (material_time * source_fps).__floor__()


def plan_video(
    duration: Fraction,
    cuts: Sequence[Cut],
    pieces: dict[str, Piecewise],
    source_fps: dict[str, Fraction],
) -> list[FrameRecord]:
    """One :class:`FrameRecord` per output frame, in order, with no gaps."""
    ordered = sorted(cuts, key=lambda c: c.start)
    n_frames = frame_count(duration, OUTPUT_FPS)
    records: list[FrameRecord] = []
    for n in range(n_frames):
        t = frame_center(n, OUTPUT_FPS)
        cut = cut_at(ordered, t)
        if cut is None:
            records.append(FrameRecord(n, t, None, None, None, None, "no-camera"))
            continue
        piece = pieces.get(cut.camera)
        fps = source_fps.get(cut.camera)
        if piece is None or fps is None:
            records.append(
                FrameRecord(n, t, cut.camera, None, None, None, "outside-anchors")
            )
            continue
        m = piece.material_at(t)
        if m is None:
            records.append(
                FrameRecord(n, t, cut.camera, None, None, None, "outside-anchors")
            )
            continue
        records.append(
            FrameRecord(n, t, cut.camera, m, fps, source_frame_index(m, fps), "ok")
        )
    return records


def _split_samples(event_times: list[Fraction], rate: int, n_samples: int) -> list[int]:
    """Output-sample split positions for the given output-time events.

    Index s is the first sample whose center (s + 1/2)/rate is >= the event,
    i.e. ceil(event*rate - 1/2), which keeps block boundaries consistent with
    the frame-style half-open convention.
    """
    import math

    splits = {0, n_samples}
    for e in event_times:
        # Cell s is centered at (s + 1/2)/rate.  The first cell whose center
        # is >= event e has s = ceil(e*rate - 1/2).
        s = math.ceil(e * rate - Fraction(1, 2))
        if 0 < s < n_samples:
            splits.add(s)
    return sorted(splits)


def plan_audio(duration: Fraction, master: Piecewise, rate: int) -> list[AudioBlock]:
    """Partition every output sample into affine source blocks or silence.

    Blocks are split at master anchor boundaries and at each source-integer
    second so the report stays human-readable; splitting never changes the
    per-sample math.
    """
    import math

    # Sample s is centered at (s + 1/2)/rate: the count is
    # ceil(duration*rate - 1/2), exactly the same half-open rule as video.
    n_samples = math.ceil(duration * rate - Fraction(1, 2))
    events = sorted({a.output for a in master.anchors})
    # Material-integer seconds crossed by the mapped range (for report only).
    lo_mat = master.mat_start
    hi_mat = master.mat_end
    for k in range(int(lo_mat) + 1, int(hi_mat) + 1):
        mat_t = Fraction(k)
        # inverse: output = (mat - b)/a per segment; do it from anchors.
        for a0, a1 in zip(master.anchors, master.anchors[1:]):
            if a0.material <= mat_t <= a1.material:
                u = a0.output + (mat_t - a0.material) * (a1.output - a0.output) / (
                    a1.material - a0.material
                )
                events.append(u)
    splits = _split_samples(events, rate, n_samples)
    blocks: list[AudioBlock] = []
    for s0, s1 in zip(splits, splits[1:]):
        # Resolve at the block center; affine segments never cross an anchor
        # inside a block because anchor times are themselves split points.
        t_mid = (Fraction(s0 + s1, 2) + Fraction(1, 2)) / rate
        out0 = (Fraction(s0) + Fraction(1, 2)) / rate
        out1 = (Fraction(s1) - Fraction(1, 2)) / rate
        m0 = master.material_at(out0)
        m1 = master.material_at(out1)
        # A block is either wholly covered or wholly uncovered: an anchor on
        # the span boundary falls on a cell center (split point), so the
        # block's own center decides; silence blocks carry no material time.
        mid_ok = master.material_at(t_mid) is not None
        if not mid_ok:
            blocks.append(
                AudioBlock(s0, s1, out0, out1, None, None, True,
                           "outside-anchors")
            )
        else:
            blocks.append(AudioBlock(s0, s1, out0, out1, m0, m1, False, "ok"))
    return blocks


def anchors_from_pairs(pairs) -> list[Anchor]:
    """Build validated anchors from raw (material, output) request data."""
    anchors = [
        Anchor(parse_q(m, "material_time"), parse_q(o, "output_time"))
        for m, o in pairs
    ]
    if len(anchors) < 2:
        raise ValidationError("at least two anchors are required")
    mats = [a.material for a in anchors]
    outs = [a.output for a in anchors]
    if any(b <= a for a, b in zip(mats, mats[1:])):
        raise ValidationError("material times must be strictly increasing")
    if any(b <= a for a, b in zip(outs, outs[1:])):
        raise ValidationError("output times must be strictly increasing")
    return anchors

"""Timeline model: anchor maps, camera cuts, exact rational resolution.

Coordinates
-----------
* ``T``   - master timeline ("成片") time in seconds, starts at 0.
* ``u``   - material ("素材") time in seconds, measured on the **displayed
            timestamp clock** of that asset.

Every asset gets >= 2 strictly increasing anchors ``(u, T)``.  Between two
anchors mapping is one linear (Fraction-exact) segment; outside the anchor
span the asset is considered to have NO material (holes -> black / silence,
never a repeated edge frame/sample).
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

from .time import FPS, SAMPLE_RATE, Q


class EditError(ValueError):
    pass


@dataclass(frozen=True)
class Anchor:
    material: Fraction
    timeline: Fraction


class AnchorMap:
    """Piecewise linear, Fraction-exact map timeline T -> material u."""

    def __init__(self, anchors: list[Anchor]):
        if len(anchors) < 2:
            raise EditError("at least 2 anchors are required")
        pts = sorted(anchors, key=lambda a: a.timeline)
        for a, b in zip(pts, pts[1:]):
            if not (a.timeline < b.timeline):
                raise EditError("anchor timeline times must be strictly increasing")
            if not (a.material < b.material):
                raise EditError("anchor material times must be strictly increasing")
        self.anchors = pts

    @property
    def t_start(self) -> Fraction:
        return self.anchors[0].timeline

    @property
    def t_end(self) -> Fraction:
        return self.anchors[-1].timeline

    def segment(self, T: Fraction) -> Optional[tuple[int, Fraction, Fraction]]:
        """Return ``(segment_index, u_at_T, slope dT/du)`` or None.

        Coverage is the half-open interval [t0, tN).  The final instant
        tN (one frame beyond the last displayed frame) is not covered.
        """
        if T < self.anchors[0].timeline or T >= self.anchors[-1].timeline:
            return None
        for k in range(len(self.anchors) - 1):
            a, b = self.anchors[k], self.anchors[k + 1]
            # half-open: leftmost segment whose right end exceeds T
            if a.timeline <= T < b.timeline:
                du = b.material - a.material
                dt = b.timeline - a.timeline
                u = a.material + du * (T - a.timeline) / dt
                return k, u, dt / du
        return None

    def material_at(self, T: Fraction) -> Optional[Fraction]:
        seg = self.segment(T)
        return None if seg is None else seg[1]

    def to_json(self) -> list[dict]:
        return [
            {"material": str(a.material), "timeline": str(a.timeline)}
            for a in self.anchors
        ]


@dataclass(frozen=True)
class Cut:
    start: Fraction   # included
    end: Fraction     # excluded
    asset_id: int

    def contains(self, T: Fraction) -> bool:
        return self.start <= T < self.end


@dataclass
class EditState:
    """A fully validated editing snapshot."""

    duration: Fraction
    assets: dict                       # asset_id -> asset row dict
    anchors: dict[int, AnchorMap]      # asset_id -> AnchorMap
    cuts: list[Cut]
    video_asset_ids: list[int]
    audio_asset_id: Optional[int]
    width: int
    height: int

    def video_resolve(self, T: Fraction):
        """(asset_id, material_time, segment_index) covering T, else None."""
        for cut in self.cuts:
            if cut.contains(T):
                amap = self.anchors[cut.asset_id]
                seg = amap.segment(T)
                if seg is None:
                    return None  # camera chosen but its material ran out
                return cut.asset_id, seg[1], seg[0]
        return None

    def audio_resolve(self, T: Fraction):
        if self.audio_asset_id is None:
            return None
        amap = self.anchors[self.audio_asset_id]
        seg = amap.segment(T)
        if seg is None:
            return None
        return self.audio_asset_id, seg[1], seg[0]


# ---------------------------------------------------------------------------
# Validation / construction from API payload
# ---------------------------------------------------------------------------

def _parse_anchors(raw: list, asset_id: int) -> AnchorMap:
    if not isinstance(raw, list) or len(raw) < 2:
        raise EditError(f"asset {asset_id}: at least 2 anchors required")
    pts: list[Anchor] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise EditError(f"asset {asset_id}: anchor {i} is invalid")
        try:
            u = Q(item.get("material"),
                  f"asset {asset_id} anchor {i} material",
                  allow_negative=True)
            T = Q(item.get("timeline"),
                  f"asset {asset_id} anchor {i} timeline")
        except ValueError as e:
            raise EditError(str(e)) from e
        pts.append(Anchor(u, T))
    return AnchorMap(pts)


def build_edit_state(payload: dict, assets: dict) -> EditState:
    """Validate an edit payload against known asset rows.

    ``assets`` maps id -> row (with width/height/kind/material0/clock...).
    """
    try:
        duration = Q(payload.get("duration", 0), "duration")
    except ValueError as e:
        raise EditError(str(e)) from e
    if duration <= 0:
        raise EditError("duration must be > 0")

    # duration is expressed on the output grids: whole frames and samples.
    nframes = duration * FPS
    nsamples = duration * SAMPLE_RATE
    if nframes.denominator != 1:
        raise EditError("duration must be a whole number of 1/30s frames")
    if nsamples.denominator != 1:
        raise EditError("duration must be a whole number of 1/48000s samples")

    raw_anchors = payload.get("anchors", {})
    anchors: dict[int, AnchorMap] = {}
    video_ids: list[int] = []
    audio_id: Optional[int] = None

    for aid, row in assets.items():
        if str(aid) not in raw_anchors:
            raise EditError(f"asset {aid}: anchors missing")
        amap = _parse_anchors(raw_anchors[str(aid)], aid)
        # material span: file frame/sample 0 displays m0, the last instant
        # covered by the file is m0 + clock * wall_duration.
        m0 = Fraction(row["material0"])
        span = Fraction(row["material_span"])
        clock = Fraction(row["clock_num"], row["clock_den"])
        u_end = m0 + clock * span
        for a in amap.anchors:
            if not (m0 <= a.material <= u_end):
                raise EditError(
                    f"asset {aid}: anchor material {a.material} outside "
                    f"asset clock range [{m0}, {u_end}]"
                )
        anchors[aid] = amap
        if row["kind"] == "video":
            video_ids.append(aid)
        else:
            if audio_id is not None:
                raise EditError("more than one audio asset")
            audio_id = aid

    cuts: list[Cut] = []
    for i, item in enumerate(payload.get("cuts", [])):
        if not isinstance(item, dict):
            raise EditError(f"cut {i} invalid")
        aid = item.get("asset_id")
        if aid not in assets or assets[aid]["kind"] != "video":
            raise EditError(f"cut {i}: unknown video asset {aid}")
        try:
            s = Q(item.get("start"), f"cut {i} start")
            e = Q(item.get("end"), f"cut {i} end")
        except ValueError as ex:
            raise EditError(str(ex)) from ex
        if not (0 <= s < e):
            raise EditError(f"cut {i}: need 0 <= start < end")
        if e > duration:
            raise EditError(f"cut {i}: end beyond timeline duration")
        cuts.append(Cut(s, e, aid))

    cuts.sort(key=lambda c: c.start)
    for a, b in zip(cuts, cuts[1:]):
        if b.start < a.end:
            raise EditError("camera cuts must not overlap")

    if video_ids:
        w = max(int(assets[i]["width"]) for i in video_ids)
        h = max(int(assets[i]["height"]) for i in video_ids)
    else:
        w, h = 320, 240

    return EditState(
        duration=duration,
        assets=assets,
        anchors=anchors,
        cuts=cuts,
        video_asset_ids=video_ids,
        audio_asset_id=audio_id,
        width=w,
        height=h,
    )

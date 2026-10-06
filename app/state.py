"""Typed edit state, derived from the DB row document, and export snapshots."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Optional

from .db import load_json_state
from .mapping import Anchor, Cut, Piecewise, ValidationError, validate_cuts
from .rationals import parse_q

CAMERA_ROLES = ("A", "B")
MASTER_ROLE = "master"
ALL_ROLES = ("A", "B", "master")


@dataclass
class AssetMeta:
    role: str
    kind: str
    path: Path
    fps: Optional[Fraction] = None
    frames_dir: Optional[Path] = None
    frame_count: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    samples: Optional[int] = None
    pcm_path: Optional[Path] = None
    uploaded_version: int = 0


@dataclass
class State:
    duration: Fraction
    edit_version: int
    assets: dict[str, AssetMeta]
    anchors: dict[str, list[Anchor]]
    cuts: list[Cut]
    canvas: tuple[int, int]
    errors: list[str] = field(default_factory=list)

    def piecewise(self) -> dict[str, Piecewise]:
        out = {}
        for role, ans in self.anchors.items():
            if len(ans) >= 2:
                out[role] = Piecewise(ans)
        return out

    def source_fps(self) -> dict[str, Fraction]:
        return {r: a.fps for r, a in self.assets.items() if a.fps is not None}

    def ready_for_export(self) -> tuple[bool, list[str]]:
        problems: list[str] = []
        for role in CAMERA_ROLES:
            if role not in self.assets:
                problems.append(f"camera {role} video missing")
        if MASTER_ROLE not in self.assets:
            problems.append("master WAV missing")
        for role in ALL_ROLES:
            if len(self.anchors.get(role, ())) < 2:
                problems.append(f"{role}: at least two anchors required")
        if self.duration <= 0:
            problems.append("duration must be > 0")
        return (not problems and not self.errors), (problems + self.errors)


def _even(x: int) -> int:
    return x if x % 2 == 0 else x - 1


def compute_canvas(assets: dict[str, AssetMeta]) -> tuple[int, int]:
    vids = [a for a in assets.values() if a.kind == "video" and a.width and a.height]
    if not vids:
        return (0, 0)
    return (_even(max(a.width for a in vids)), _even(max(a.height for a in vids)))


def load_state(conn: sqlite3.Connection, data_dir: Path) -> State:
    doc = load_json_state(conn)
    data_dir = Path(data_dir)
    assets: dict[str, AssetMeta] = {}
    for role, d in doc["assets"].items():
        assets[role] = AssetMeta(
            role=role,
            kind=d["kind"],
            path=Path(d["path"]),
            fps=parse_q(d["fps"], f"{role}.fps") if d.get("fps") else None,
            frames_dir=Path(d["frames_dir"]) if d.get("frames_dir") else None,
            frame_count=d.get("frame_count"),
            width=d.get("width"),
            height=d.get("height"),
            sample_rate=d.get("sample_rate"),
            channels=d.get("channels"),
            samples=d.get("samples"),
            pcm_path=Path(d["pcm_path"]) if d.get("pcm_path") else None,
            uploaded_version=d.get("uploaded_version", 0),
        )
    anchors: dict[str, list[Anchor]] = {}
    anchor_errors: list[str] = []
    for role, items in doc["anchors"].items():
        pairs = [(a["material"], a["output"]) for a in items]
        try:
            ans = [
                Anchor(parse_q(m, f"{role}.material"), parse_q(o, f"{role}.output"))
                for m, o in pairs
            ]
            if len(ans) >= 2:
                mats = [a.material for a in ans]
                outs = [a.output for a in ans]
                if any(b <= a for a, b in zip(mats, mats[1:])):
                    raise ValidationError("material times must be strictly increasing")
                if any(b <= a for a, b in zip(outs, outs[1:])):
                    raise ValidationError("output times must be strictly increasing")
            anchors[role] = ans
        except (ValueError, ValidationError) as exc:
            anchor_errors.append(f"{role}: {exc}")
            anchors[role] = []

    cuts: list[Cut] = []
    cut_errors: list[str] = []
    duration = parse_q(doc["duration"], "duration")
    try:
        for i, c in enumerate(doc["cuts"]):
            s = parse_q(c["start"], f"cuts[{i}].start")
            e = parse_q(c["end"], f"cuts[{i}].end")
            if c["camera"] not in CAMERA_ROLES:
                raise ValidationError(f"unknown camera {c['camera']!r}")
            cuts.append(Cut(s, e, c["camera"]))
        validate_cuts(cuts, duration)
    except (ValueError, ValidationError) as exc:
        cut_errors.append(str(exc))
        cuts = sorted(cuts, key=lambda c: c.start)

    return State(
        duration=duration,
        edit_version=int(doc["edit_version"]),
        assets=assets,
        anchors=anchors,
        cuts=sorted(cuts, key=lambda c: c.start),
        canvas=compute_canvas(assets),
        errors=anchor_errors + cut_errors,
    )


def snapshot(state: State) -> dict:
    """Immutable JSON document bound to an export job.

    The worker reads only this snapshot; later edits (or even new uploads)
    cannot alter what the job renders.  All paths are absolute.
    """
    assets = {}
    for role, a in state.assets.items():
        assets[role] = {
            "kind": a.kind,
            "path": str(a.path.resolve()),
            "fps": str(a.fps) if a.fps is not None else None,
            "frames_dir": str(a.frames_dir.resolve()) if a.frames_dir else None,
            "frame_count": a.frame_count,
            "width": a.width,
            "height": a.height,
            "sample_rate": a.sample_rate,
            "channels": a.channels,
            "samples": a.samples,
            "pcm_path": str(a.pcm_path.resolve()) if a.pcm_path else None,
            "uploaded_version": a.uploaded_version,
        }
    return {
        "duration": str(state.duration),
        "canvas": list(state.canvas),
        "assets": assets,
        "anchors": {
            role: [[str(a.material), str(a.output)] for a in ans]
            for role, ans in state.anchors.items()
        },
        "cuts": [[str(c.start), str(c.end), c.camera] for c in state.cuts],
    }


def snapshot_pretty(snap: dict) -> str:
    return json.dumps(snap, indent=2, ensure_ascii=False, sort_keys=True)

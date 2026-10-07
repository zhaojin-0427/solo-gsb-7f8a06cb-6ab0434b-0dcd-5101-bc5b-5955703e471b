"""Rendering: one exact path shared by preview, mapping records and export.

For every output frame ``i`` the master instant is ``T = i/30`` (half-open
frame START).  For every output sample ``n`` it is ``T = n/48000``.  The
instant is mapped through the relevant AnchorMap, and the source frame /
sample covering the mapped material instant is taken.  Uncovered instants
become a freshly generated black frame / digital zero samples.

There is intentionally no frame-buffer carry across frames: each output
frame starts as black and is either filled from the chosen camera or left
black.  Switching cameras or crossing an anchor can therefore never repeat
or freeze a frame.
"""
from __future__ import annotations

import json
import math
from fractions import Fraction
from pathlib import Path

from . import media as media_mod
from .image import black_frame, encode_png, fit_to_canvas
from .time import FPS, SAMPLE_RATE, frac_str
from .timeline import EditState


def source_wall_index(asset_row: dict, u: Fraction) -> int:
    """Material-clock time u -> wall frame index of the source file."""
    fps = Fraction(asset_row["fps_num"], asset_row["fps_den"])
    clock = Fraction(asset_row["clock_num"], asset_row["clock_den"])
    m0 = Fraction(asset_row["material0"])
    # displayed(u_wall) = material0 + clock*u_wall
    wall_t = (u - m0) / clock
    idx = wall_t * fps
    # u is strictly inside a material span that itself ends ON a frame
    # boundary, so this floor is always a valid index; guard anyway.
    return math.floor(idx)


def source_sample_index(asset_row: dict, u: Fraction) -> int:
    """Material-clock time u -> 48 kHz interleaved source frame index."""
    clock = Fraction(asset_row["clock_num"], asset_row["clock_den"])
    m0 = Fraction(asset_row["material0"])
    wall_t = (u - m0) / clock
    return math.floor(wall_t * SAMPLE_RATE)


def render_video_frame(state: EditState, frame_index: int,
                       decoders: dict | None = None) -> tuple[bytes, dict]:
    """Return (rgb24 at output WxH, provenance dict) for output frame i."""
    T = Fraction(frame_index, FPS)
    w, h = state.width, state.height
    hit = state.video_resolve(T)
    if hit is None:
        return black_frame(w, h), {
            "output_frame": frame_index,
            "target_time": frac_str(T),
            "source": "black",
        }
    aid, u, seg = hit
    row = state.assets[aid]
    idx = source_wall_index(row, u)
    if decoders is None:
        decoders = {}
    sv = decoders.get(("v", aid))
    if sv is None:
        fps = Fraction(row["fps_num"], row["fps_den"])
        sv = media_mod.get_source_video(
            aid, row["path"], fps, int(row["nframes"]),
            int(row["width"]), int(row["height"]),
        )
        decoders[("v", aid)] = sv
    frame = sv.get_frame(idx)
    if frame is None:
        # Explicit hole - never the previous frame.
        return black_frame(w, h), {
            "output_frame": frame_index,
            "target_time": frac_str(T),
            "source": "black",
            "reason": "source_index_out_of_range",
        }
    rgb = fit_to_canvas(frame, int(row["width"]), int(row["height"]), w, h)
    return rgb, {
        "output_frame": frame_index,
        "target_time": frac_str(T),
        "source": "video",
        "asset_id": aid,
        "material_time": frac_str(u),
        "source_frame_index": idx,
        "anchor_segment": seg,
    }


# ---------------------------------------------------------------------------
# Audio.  Per output-sample nearest-index lookup; holes are zero samples.
# ---------------------------------------------------------------------------

def _audio_decoder(state: EditState, aid: int, decoders: dict):
    sa = decoders.get(("a", aid))
    if sa is None:
        row = state.assets[aid]
        sa = media_mod.get_source_audio(
            aid, row["path"], int(row["channels"]),
            Fraction(row["clock_num"], row["clock_den"]),
            Fraction(row["material0"]),
            Fraction(row["material_span"]),
        )
        decoders[("a", aid)] = sa
    return sa


def render_audio_samples(state: EditState, start: int, count: int,
                         decoders: dict | None = None) -> tuple[bytearray, list[dict]]:
    """Render output audio FRAMES [start, start+count) at 48 kHz.

    A "frame" is one interleaved group of ``channels`` 16-bit samples, so
    this function is channel-count agnostic.  Returns interleaved s16le
    bytes and a mapping record per maximal run of identical provenance
    (a silence hole, or one contiguous run of source frame indices).
    Frame instants live on the 48k grid, so runs tile the output exactly
    without splitting or duplicating a frame.
    """
    aid = state.audio_asset_id
    if aid is None:
        ch = 1
        return (bytearray(b"\x00\x00" * count * ch),
                [_audio_record(0, start, start + count, None, None, None)])
    if decoders is None:
        decoders = {}
    sa = _audio_decoder(state, aid, decoders)
    row = state.assets[aid]
    # Interleaved frame count == per-channel sample count (48k * seconds).
    # probe_audio stores nsamples from duration*in_rate on that basis.
    n_src = int(row["nsamples"])
    ch = sa.channels
    fb = ch * 2

    # Classify each output frame: (kind, src_frame_index).
    kinds: list[tuple[str, int]] = []
    for n in range(start, start + count):
        T = Fraction(n, SAMPLE_RATE)
        hit = state.audio_resolve(T)
        if hit is None:
            kinds.append(("hole", -1))
        else:
            _, u, _seg = hit
            sidx = source_sample_index(row, u)
            if 0 <= sidx < n_src:
                kinds.append(("src", sidx))
            else:
                kinds.append(("hole", -1))

    pcm = sa._decode()
    out = bytearray(fb * count)
    records: list[dict] = []

    def flush(r0: int, r1: int, kind: str, idx0: int | None, idx1: int | None):
        if r1 <= r0:
            return
        records.append(_audio_record(
            len(records), start + r0, start + r1,
            aid if kind == "src" else None, idx0, idx1))

    # Walk maximal runs.  A source run continues only while source frame
    # indices advance by exactly 1 (any speed change either keeps the +1
    # relationship or naturally starts a new run).
    pos = 0
    total = len(kinds)
    while pos < total:
        kind, idx = kinds[pos]
        r1 = pos + 1
        if kind == "src":
            while r1 < total and kinds[r1] == ("src", idx + (r1 - pos)):
                r1 += 1
            s1 = idx + (r1 - pos)
            chunk = pcm[idx * fb:s1 * fb]
            out[pos * fb:pos * fb + len(chunk)] = chunk
            flush(pos, r1, "src", idx, s1)
        else:
            while r1 < total and kinds[r1][0] == "hole":
                r1 += 1
            flush(pos, r1, "hole", None, None)
        pos = r1

    return out, records


def _audio_record(order: int, out0: int, out1: int, aid, s0, s1) -> dict:
    rec = {
        "order": order,
        "output_sample_start": out0,
        "output_sample_end": out1,
        "output_time_start": frac_str(Fraction(out0, SAMPLE_RATE)),
        "output_time_end": frac_str(Fraction(out1, SAMPLE_RATE)),
    }
    if aid is None:
        rec["source"] = "silence"
    else:
        rec["source"] = "audio"
        rec["asset_id"] = aid
        rec["source_sample_start"] = s0
        rec["source_sample_end"] = s1
        rec["source_time_start"] = frac_str(Fraction(s0, SAMPLE_RATE))
        rec["source_time_end"] = frac_str(Fraction(s1, SAMPLE_RATE))
    return rec


# ---------------------------------------------------------------------------
# Full-render mapping records (same code path as export)
# ---------------------------------------------------------------------------

def build_video_records(state: EditState) -> list[dict]:
    nframes = int(state.duration * FPS)
    recs = []
    decoders: dict = {}
    for i in range(nframes):
        _, rec = render_video_frame(state, i, decoders)
        recs.append(rec)
    return recs


def _merge_audio_records(recs: list[dict]) -> list[dict]:
    """Coalesce adjacent provenance runs (also across chunk boundaries).

    Two runs merge when they are the same kind (silence, or the same
    audio asset), tile contiguously in output frames, and - for audio -
    the source frame indices continue by exactly 1.  Nothing is reordered.
    """
    merged: list[dict] = []
    for r in recs:
        if merged:
            p = merged[-1]
            contiguous_out = p["output_sample_end"] == r["output_sample_start"]
            if r["source"] == "silence" and p["source"] == "silence" and contiguous_out:
                p["output_sample_end"] = r["output_sample_end"]
                p["output_time_end"] = r["output_time_end"]
                continue
            if (r["source"] == "audio" and p["source"] == "audio"
                    and r["asset_id"] == p["asset_id"] and contiguous_out
                    and p["source_sample_end"] == r["source_sample_start"]):
                p["output_sample_end"] = r["output_sample_end"]
                p["output_time_end"] = r["output_time_end"]
                p["source_sample_end"] = r["source_sample_end"]
                p["source_time_end"] = r["source_time_end"]
                continue
        merged.append(dict(r))
    for i, r in enumerate(merged):
        r["order"] = i
    return merged


def build_audio_records(state: EditState, chunk: int = 8192) -> list[dict]:
    ns = int(state.duration * SAMPLE_RATE)
    chunk_recs: list[dict] = []
    decoders: dict = {}
    for s in range(0, ns, chunk):
        c = min(chunk, ns - s)
        _, recs = render_audio_samples(state, s, c, decoders)
        chunk_recs.extend(recs)
    return _merge_audio_records(chunk_recs)


def write_mapping_json(path: str | Path, state: EditState,
                       video_records: list[dict], audio_records: list[dict],
                       edit_snapshot: dict) -> None:
    payload = {
        "format": {
            "fps_num": 30, "fps_den": 1,
            "sample_rate": 48000,
            "width": state.width, "height": state.height,
            "duration": frac_str(state.duration),
            "frames": int(state.duration * FPS),
            "samples": int(state.duration * SAMPLE_RATE),
        },
        "edit_snapshot": edit_snapshot,
        "video": video_records,
        "audio": audio_records,
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=0))


def render_png_preview(state: EditState, frame_index: int) -> tuple[bytes, dict]:
    rgb, rec = render_video_frame(state, frame_index)
    return encode_png(rgb, state.width, state.height), rec

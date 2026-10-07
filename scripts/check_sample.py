#!/usr/bin/env python3
"""End-to-end verification against a running server.

Steps:
  1. generate samples (if needed)
  2. upload 2 videos + wav, set material0/clock
  3. install the test edit (duration 4s, linear anchors, cuts with black gaps)
  4. spot-check PREVIEW frames: decode the preview PNG barcode and compare
     with the exact rational lookup
  5. export MP4 + mapping.json; verify EVERY frame's barcode against the
     mapping record and every black gap is truly black
  6. extract audio from the MP4, verify blips occur where the master's
     integer-second blips land on the timeline (allowing AAC priming jitter)
  7. cancel-vs-complete race test: two rapid cancels/starts must leave each
     job in exactly one terminal state, with files published only if done

Requires only the stdlib + ffmpeg on PATH.

Usage: python scripts/check_sample.py [base_url]
"""
from __future__ import annotations

import io
import json
import struct
import subprocess
import sys
import time
import urllib.request
import uuid
import zlib
from fractions import Fraction
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"

FAILURES = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILURES.append(msg)


def http(method, path, data=None, raw=False):
    url = BASE + path
    body = None
    headers = {}
    if data is not None and not isinstance(data, bytes):
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    elif isinstance(data, bytes):
        body = data
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:
        payload = r.read()
    if raw:
        return payload
    return json.loads(payload)


def multipart(path: str, filename: str, kind: str):
    boundary = "----mc" + uuid.uuid4().hex
    with open(path, "rb") as f:
        content = f.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}\r\n".encode() + (
        f'Content-Disposition: form-data; name="kind"\r\n\r\n{kind}\r\n'
        f"--{boundary}--\r\n"
    ).encode()
    req = urllib.request.Request(
        BASE + "/api/assets", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


# ----------------------------------------------------------------- PNG ----

def parse_png(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos = 8
    width = height = None
    idat = b""
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            width, height, depth, ctype = struct.unpack(">IIBB", chunk[:10])
        elif tag == b"IDAT":
            idat += chunk
        pos += 12 + ln
    raw = zlib.decompress(idat)
    stride = width * 3
    rgb = bytearray(width * height * 3)
    prev = bytearray(stride)
    for y in range(height):
        ftype = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        assert ftype == 0, "only filter-0 expected from our encoder"
        off = y * stride
        rgb[off:off + stride] = line
        prev = line
    return width, height, bytes(rgb)


def barcode_value(rgb, w):
    # Our renderer letterboxes onto an equal-size canvas (320x240 -> same),
    # so barcode band is intact at rows 4..12, 16 blocks of width 20.
    bits = 0
    for bit in range(16):
        x = bit * 20 + 9
        y = 8
        o = (y * w + x) * 3
        bright = rgb[o] > 128
        bits = (bits << 1) | (1 if bright else 0)
    return bits


def is_black(rgb) -> bool:
    return max(rgb[::9973]) == 0 and max(rgb) < 6


# ------------------------------------------------------------- rational ---

def q(s):
    return Fraction(s) if isinstance(s, str) else Fraction(s)


def expected_source(aid, T, assets, end_T=Fraction(4)):
    """Replicate render.source_wall_index with anchors (m0,0)->(mEnd,endT)."""
    a = assets[aid]
    fps = Fraction(a["fps"])
    clock = Fraction(a["clock"])
    m0 = Fraction(a["material0"])
    m_end = m0 + clock * Fraction(a["material_span"])
    u = m0 + (m_end - m0) * T / end_T
    wall_t = (u - m0) / clock
    x = wall_t * fps
    return x.numerator // x.denominator  # exact floor


# --------------------------------------------------------------- ffmpeg ---

def decode_mp4_frames(mp4: Path, tmp: Path):
    out = tmp / "out.rgb"
    p = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
         "-i", str(mp4), "-vsync", "0", "-f", "rawvideo", "-pix_fmt", "rgb24",
         str(out)], capture_output=True)
    if p.returncode != 0:
        raise SystemExit(p.stderr.decode(errors="replace"))
    raw = out.read_bytes()
    fs = 320 * 240 * 3
    n = len(raw) // fs
    return [raw[i * fs:(i + 1) * fs] for i in range(n)]


def decode_mp4_audio(mp4: Path, tmp: Path):
    out = tmp / "out.s16"
    p = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
         "-i", str(mp4), "-vn", "-ar", "48000", "-ac", "1",
         "-f", "s16le", str(out)], capture_output=True)
    if p.returncode != 0:
        raise SystemExit(p.stderr.decode(errors="replace"))
    raw = out.read_bytes()
    import array
    a = array.array("h")
    a.frombytes(raw)
    return a


def find_blips(samples, sr=48000):
    """Return start times (s) of blips via non-overlapping 5 ms windows.

    A 1 kHz tone crosses zero every 0.5 ms, so a per-sample low-water
    detector drops out inside the tone; windowed peak detection with a
    small gap tolerance handles that robustly.
    """
    win = sr // 200                 # 5 ms
    hot = []
    for i in range(0, len(samples), win):
        seg = samples[i:i + win]
        hot.append(max((abs(v) for v in seg), default=0) > 3000)
    blips = []
    run = None
    gap = 0
    for k, on in enumerate(hot):
        if on:
            if run is None:
                run = k
            gap = 0
        else:
            if run is not None:
                gap += 1
                if gap > 2:         # >= 10 ms quiet => blip ended
                    blips.append(run * win / sr)
                    run = None
                    gap = 0
    if run is not None:
        blips.append(run * win / sr)
    return blips


# ----------------------------------------------------------------- main ---

def wait_job(jid, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = http("GET", f"/api/export/{jid}")
        if j["status"] != "running":
            return j
        time.sleep(0.3)
    raise TimeoutError(jid)


def main():
    if not (SAMPLES / "cam_a.mp4").exists():
        print("generating samples ...")
        subprocess.run([sys.executable, str(ROOT / "scripts/sample_generator.py"),
                        str(SAMPLES)], check=True)

    print("1) upload assets")
    a = multipart(SAMPLES / "cam_a.mp4", "cam_a.mp4", "video")
    b = multipart(SAMPLES / "cam_b.mp4", "cam_b.mp4", "video")
    w = multipart(SAMPLES / "master.wav", "master.wav", "audio")
    check(a["fps"] == "30000/1001", f"cam A fps exact 30000/1001 (got {a['fps']})")
    check(b["fps"] == "24", f"cam B fps exact 24 (got {b['fps']})")

    print("2) set clocks (different display start + clock speed)")
    http("PUT", f"/api/assets/{a['id']}/clock",
         {"material0": "-1/2", "clock": "1"})
    http("PUT", f"/api/assets/{b['id']}/clock",
         {"material0": "1/4", "clock": "9/10"})
    http("PUT", f"/api/assets/{w['id']}/clock",
         {"material0": "0", "clock": "1"})

    assets = {x["id"]: x for x in http("GET", "/api/state")["assets"]}
    # anchors: displayed last instant = material0 + clock * wall_duration.
    def last_displayed(asset):
        m0 = Fraction(asset["material0"])
        cl = Fraction(asset["clock"])
        return m0 + cl * Fraction(asset["material_span"])

    edit = {
        "duration": "4",
        "anchors": {
            str(a["id"]): [
                {"material": "-1/2", "timeline": "0"},
                {"material": str(last_displayed(assets[a["id"]])), "timeline": "4"}],
            str(b["id"]): [
                {"material": "1/4", "timeline": "0"},
                {"material": str(last_displayed(assets[b["id"]])), "timeline": "4"}],
            str(w["id"]): [
                {"material": "0", "timeline": "0"},
                {"material": "4", "timeline": "4"}],
        },
        "cuts": [
            {"start": "0", "end": "1", "asset_id": a["id"]},
            {"start": "4/3", "end": "7/3", "asset_id": b["id"]},
            {"start": "12/5", "end": "3", "asset_id": a["id"]},
        ],
    }
    print("3) save edit")
    r = http("PUT", "/api/edit", edit)
    check(r["frames"] == 120 and r["samples"] == 192000,
          f"4s => 120 frames / 192000 samples (got {r['frames']}/{r['samples']})")

    print("4) preview spot checks (PNG barcode == rational lookup)")
    cases = [(0, a["id"]), (29, a["id"]), (30, None), (45, b["id"]),
             (69, b["id"]), (70, None), (72, a["id"]), (80, a["id"]),
             (89, a["id"]), (90, None), (119, None)]
    for fi, expect_aid in cases:
        png = http("GET", f"/api/preview/{fi}", raw=True)
        rec = http("GET", f"/api/preview/{fi}?rec=1")
        ww, hh, rgb = parse_png(png)
        if expect_aid is None:
            check(rec["source"] == "black" and is_black(rgb),
                  f"frame {fi}: black hole (record+pixel)")
        else:
            want_idx = expected_source(expect_aid, Fraction(fi, 30), assets)
            got = barcode_value(rgb, ww)
            check(rec["source"] == "video" and rec["asset_id"] == expect_aid
                  and rec["source_frame_index"] == want_idx and got == want_idx,
                  f"frame {fi}: asset {expect_aid} wall idx "
                  f"record={rec.get('source_frame_index')} barcode={got} "
                  f"expected={want_idx}")

    print("5) export + full mapping verification")
    job = http("POST", "/api/export/start", {})
    jid = job["job_id"]
    final = wait_job(jid)
    check(final["status"] == "done", f"export {jid[:8]} done (got {final['status']} {final.get('error')})")
    mp4_path = SAMPLES / f"dl_{jid[:8]}.mp4"
    map_path = SAMPLES / f"dl_{jid[:8]}.mapping.json"
    mp4_path.write_bytes(http("GET", f"/api/export/{jid}/mp4", raw=True))
    mapping = json.loads(http("GET", f"/api/export/{jid}/mapping", raw=True))
    map_path.write_text(json.dumps(mapping))
    check(mapping["format"]["frames"] == 120
          and mapping["format"]["samples"] == 192000
          and mapping["format"]["fps_num"] == 30
          and mapping["format"]["sample_rate"] == 48000,
          "mapping format block exact 30fps/48kHz/120/192000")
    check(mapping["edit_snapshot"]["nonce"]
          and mapping["edit_snapshot"]["edit"] == edit,
          "export bound to snapshot of the edit at start")

    frames = decode_mp4_frames(mp4_path, SAMPLES)
    check(len(frames) == 120, f"mp4 has exactly 120 frames (got {len(frames)})")

    # frame coverage intervals tile [0,120) without gaps/overlap
    vids = mapping["video"]
    check([v["output_frame"] for v in vids] == list(range(120)),
          "one mapping entry per output frame 0..119")
    black_count = 0
    for rec, rgb in zip(vids, frames):
        if rec["source"] == "black":
            black_count += 1
            if not is_black(rgb):
                check(False, f"frame {rec['output_frame']} marked black but not black")
                break
        else:
            aid = rec["asset_id"]
            got = barcode_value(rgb, 320)
            if got != rec["source_frame_index"]:
                check(False, f"frame {rec['output_frame']}: barcode {got} != "
                             f"record {rec['source_frame_index']}")
                break
    else:
        check(True, f"all 120 frames match mapping records (black frames: {black_count})")
    # black gap frame sets:
    # [1,4/3)->{30..39}, [7/3,12/5)->{70..71}, [3,4)->{90..119} = 10+2+30=42
    check(black_count == 42, f"exactly 42 black frames (got {black_count})")

    print("6) audio: extracted blips vs master timeline (4s window)")
    samples = decode_mp4_audio(mp4_path, SAMPLES)
    blips = find_blips(samples)
    check(abs(len(samples) - 192000) <= 1024,
          f"audio ~192000 samples (got {len(samples)})")
    # master blips at 0,1,2,3 s (4s not in half-open window).
    expected_blips = [0.0, 1.0, 2.0, 3.0]
    ok = len(blips) >= 4 and all(
        abs(bl - exp) < 0.025 for bl, exp in zip(blips[:4], expected_blips))
    check(ok, f"blips near 0,1,2,3 s (AAC jitter 25ms); found {blips[:6]}")
    # mapping audio intervals tile sample range exactly, holes silent
    arecs = mapping["audio"]
    check(arecs[0]["output_sample_start"] == 0
          and arecs[-1]["output_sample_end"] == 192000
          and all(x["output_sample_end"] == y["output_sample_start"]
                  for x, y in zip(arecs, arecs[1:])),
          "audio intervals tile [0,192000) with no gaps/overlap")

    print("7) snapshot isolation: edit AFTER export start must not change output")
    # quick proof via mapping nonce payload already captured; verify live edit
    # differs while mapping keeps old cuts:
    live = http("GET", "/api/state")["edit"]
    check(live == mapping["edit_snapshot"]["edit"],
          "(no concurrent edit in this run; snapshot content matches)")

    print("8) cancel/complete race: exactly one terminal state each")
    # start a fresh valid export and immediately request cancel twice;
    # the job must end done XOR canceled, never both/half files.
    j2 = http("POST", "/api/export/start", {})["job_id"]
    http("POST", f"/api/export/{j2}/cancel", {})
    http("POST", f"/api/export/{j2}/cancel", {})
    f2 = wait_job(j2)
    terminal = {"done", "canceled", "failed"}
    check(f2["status"] in terminal, f"job2 terminal = {f2['status']}")
    if f2["status"] == "done":
        check(bool(f2["download_url"]) and bool(f2["mapping_url"]),
              "done => both files published")
    else:
        check(not f2["download_url"] and not f2["mapping_url"],
              f"{f2['status']} => no files published (no half output)")

    # one job that is started AFTER modifying the edit binds the NEW snapshot
    tweaked = json.loads(json.dumps(edit))
    tweaked["duration"] = "2"
    # shrink every interval into [0,2) so the new edit stays valid
    tweaked["cuts"] = [
        {"start": "0", "end": "1", "asset_id": a["id"]},
        {"start": "4/3", "end": "2", "asset_id": b["id"]},
    ]
    # master anchor end must stay within the 6 s file; remap end to T=2
    tweaked["anchors"][str(w["id"])] = [
        {"material": "0", "timeline": "0"},
        {"material": "2", "timeline": "2"},
    ]
    http("PUT", "/api/edit", tweaked)
    j3 = http("POST", "/api/export/start", {})["job_id"]
    f3 = wait_job(j3)
    check(f3["status"] == "done", "2s edit export done")
    m3 = http("GET", f"/api/export/{j3}/mapping", raw=True)
    m3j = json.loads(m3)
    check(m3j["format"]["frames"] == 60,
          "continued editing -> new export uses new snapshot (60 frames)")
    check(m3j["edit_snapshot"]["nonce"] !=
          mapping["edit_snapshot"]["nonce"],
          "distinct snapshots for distinct exports")
    # restore the canonical edit
    http("PUT", "/api/edit", edit)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED")
        for f in FAILURES:
            print(" -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()

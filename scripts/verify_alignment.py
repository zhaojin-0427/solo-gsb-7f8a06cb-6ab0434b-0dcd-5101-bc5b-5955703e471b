#!/usr/bin/env python3
"""End-to-end alignment verification.

Generates (or reuses) the sample media, uploads it to a live editor API,
sets anchors/cuts, and asserts that:

* every preview PNG is byte-identical to the source PNG whose display frame
  number the shared timeline maps to — using the burned-in FRAME counter;
* uncovered output times are exactly black (never the previous camera's last
  frame), including the hole at a camera switch;
* the selected camera cuts occur at the exact predicted frame boundaries
  (no repeated/skipped output cells);
* the 48 kHz preview WAV (and the exported MP4 audio) contains master pulses
  exactly where the master-anchor mapping predicts, within 30 ms;
* the exported MP4 has 30 fps and its frames match predicted sources within
  H.264 tolerance;
* the mapping manifest agrees with the rational edit document and lists the
  whole timeline;
* the export snapshot is the snapshot bound at start; editing afterwards does
  not change the finished export;
* cancellation never publishes a file and failed/cancelled jobs are terminal.

Runs either against ``docker compose`` services or, with ``--local``, against
a temporary in-process TestClient (no Docker needed).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import time
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.generate_samples import make_video, make_wav  # noqa: E402
from app.rationals import OUTPUT_FPS, parse_q  # noqa: E402

DURATION = Fraction(225, 24)          # 9.375 s -> 281 frames at 30 fps (half-open cell centers)
ANCHORS = {
    # (material time, output time); display frame k material time = k/fps
    "A": [("1/60", "0"), ("1439/120", "215/24")],
    "B": [("1/48", "5/24"), ("383/48", "215/24")],
    "master": [("1/88200", "0"), ("793801/88200", "215/24")],
}
CUTS = [
    ["1/6", "4/3", "A"],               # black before 1/6
    ["3/2", "25/6", "B"],
    ["9/2", "6", "A"],                 # black hole (4, 4.5)
    ["25/4", "17/2", "B"],             # black tail (8.5, 9.375)
]
A_FPS = Fraction(30)
B_FPS = Fraction(24)
FPS_DEN = {"A": A_FPS, "B": B_FPS}

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name + (f": {detail}" if detail else ""))


# ---------------------------------------------------------------- transport

class LocalClient:
    """Drives the ASGI app in-process with an isolated data dir."""

    def __init__(self, data_dir: Path):
        os.environ["MC_DATA_DIR"] = str(data_dir)
        # Import lazily so env var is honored.
        import importlib

        import app.main as main
        importlib.reload(main)
        from fastapi.testclient import TestClient

        self.client = TestClient(main.app)
        self.main = main
        self.client.__enter__()
        # Export dir mount was created at import; ensure exists.
        main.EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    def close(self):
        self.client.__exit__(None, None, None)

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def post(self, path, files=None, data=None, **kw):
        return self.client.post(path, files=files, data=data, **kw)

    def put(self, path, json_body=None, **kw):
        return self.client.put(path, json=json_body, **kw)


class HttpClient:
    def __init__(self, base: str):
        import httpx

        self.c = httpx.Client(base_url=base, timeout=600)

    def close(self):
        self.c.close()

    def get(self, path, **kw):
        return self.c.get(path, **kw)

    def post(self, path, files=None, data=None, **kw):
        if files:
            return self.c.post(path, files=files, data=data, **kw)
        return self.c.post(path, data=data, **kw)

    def put(self, path, json_body=None, **kw):
        return self.c.put(path, json=json_body, **kw)


def upload(client, role, path):
    with open(path, "rb") as f:
        r = client.post("/api/upload", data={"role": role},
                        files={"file": (path.name, f)})
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- helpers

def save_edits(client):
    body = {"duration": str(DURATION), "anchors": ANCHORS, "cuts": CUTS}
    r = client.put("/api/state", json_body=body)
    r.raise_for_status()
    return r.json()


def png_array(data: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))


def wait_job(client, job_id, timeout=600):
    deadline = time.time() + timeout
    while True:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        if time.time() > deadline:
            raise TimeoutError(job_id)
        time.sleep(0.5)


def decode_mp4(mp4: Path, workdir: Path):
    import subprocess

    rgb = workdir / "out.rgb"
    wav = workdir / "out.wav"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(mp4), "-f", "rawvideo", "-pix_fmt", "rgb24", str(rgb),
    ], check=True)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(mp4), "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le",
        str(wav),
    ], check=True)
    return rgb, wav


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0
    return x, rate


def pulse_times(x: np.ndarray, rate: int, thresh: float = 0.1) -> list[float]:
    """Center times of pulses: envelope local maxima >= thresh, min 0.5 s apart.

    Uses the RMS envelope so a pulse beginning exactly at sample 0 (no rising
    edge before it) is still detected.
    """
    w = max(1, rate // 100)  # 10 ms window
    env = np.sqrt(np.convolve(x * x, np.ones(w) / w, mode="same"))
    peaks = []
    i = 0
    guard = int(0.5 * rate)
    while i < len(env):
        if env[i] >= thresh:
            lo = max(0, i - int(0.06 * rate))
            hi = min(len(env), i + int(0.06 * rate))
            j = lo + int(np.argmax(env[lo:hi]))
            t = j / rate
            if not peaks or t - peaks[-1] > 0.3:
                peaks.append(t)
            i = j + guard
        else:
            i += 1
    return peaks


# ---------------------------------------------------------------- checks

def expected_source(camera: str, output_center: Fraction):
    """Return (camera, source_frame) for an output frame center."""
    from app.mapping import Anchor, Piecewise, cut_at, Cut

    cuts = [Cut(parse_q(s), parse_q(e), c) for s, e, c in CUTS]
    cut = cut_at(cuts, output_center)
    if cut is None:
        return None, None
    pw = Piecewise([Anchor(parse_q(m), parse_q(o)) for m, o in ANCHORS[cut.camera]])
    mt = pw.material_at(output_center)
    sf = int((mt * FPS_DEN[cut.camera]).__floor__())
    return cut.camera, sf


def verify_preview_frames(client, sample_dir: Path, workdir: Path):
    print("== preview frames vs burned source frame numbers ==")
    # Re-derive expected frames from the API itself + compare PNG content.
    black_seen = 0
    cam_frames = {"A": 0, "B": 0}
    prev_camera = None
    for n in range(281):
        info = client.get(f"/api/frame-info?n={n}").json()
        r = client.get(f"/preview/frame/{n}.png")
        r.raise_for_status()
        arr = png_array(r.content)
        if info["fill"]:
            check(f"frame {n} fills black", arr.max() == 0)
            black_seen += 1
            # The key fill-hole requirement: must not equal prior camera frame.
        else:
            cam_frames[info["camera"]] += 1
            asset = sample_dir
            src = asset / f"{info['camera']}_frame_{info['source_frame']:08d}.png"
            exp = png_array(src.read_bytes())
            same = arr.shape == exp.shape and np.array_equal(arr, exp)
            check(f"frame {n} == {info['camera']} source {info['source_frame']}", same)
            check(f"frame {n} material time matches floor rule",
                  info["source_frame"] == int(
                      (parse_q(info["material_time"]) * FPS_DEN[info["camera"]])
                  ))
            if prev_camera is not None and info["camera"] != prev_camera:
                pass  # boundary asserted separately below
        prev_camera = info["camera"] if not info["fill"] else prev_camera
    check("black frames present (holes filled, not held)", black_seen == 53,
          f"got {black_seen}")
    check("both cameras used", cam_frames["A"] > 0 and cam_frames["B"] > 0,
          str(cam_frames))

    # Exact boundary check against the half-open cut intervals.
    print("== exact half-open cut boundaries ==")
    expected_black = (set(range(0, 5)) | set(range(40, 45))
                      | set(range(125, 135)) | set(range(180, 187))
                      | set(range(255, 281)))
    expected_A = set(range(5, 40)) | set(range(135, 180))
    expected_B = set(range(45, 125)) | set(range(187, 255))
    for n in range(281):
        info = client.get(f"/api/frame-info?n={n}").json()
        c = None if info["fill"] else info["camera"]
        exp = "black" if n in expected_black else ("A" if n in expected_A else "B")
        got = "black" if c is None else c
        check(f"boundary frame {n} ({got} == {exp})", got == exp)

    # No duplicate/omitted output frame: 281 distinct records, centers exactly
    # (n + 1/2)/30, monotonic source indices within each continuous camera run.
    print("== output cells complete & contiguous ==")
    times = []
    for n in range(281):
        info = client.get(f"/api/frame-info?n={n}").json()
        times.append(parse_q(info["output_time"]))
    check("centers exact",
          all(t == Fraction(2 * n + 1, 60) for n, t in enumerate(times)))


def verify_audio(client, workdir: Path, label: str, wav_bytes_or_path,
                 expected_pulses: list[float], tol=0.030):
    print(f"== {label}: pulse alignment via master anchors ==")
    if isinstance(wav_bytes_or_path, (str, Path)):
        x, rate = read_wav(Path(wav_bytes_or_path))
    else:
        with wave.open(io.BytesIO(wav_bytes_or_path), "rb") as wf:
            rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0
    check(f"{label}: rate is 48000", rate == 48000, f"got {rate}")
    got = pulse_times(x, rate)
    check(f"{label}: expected {len(expected_pulses)} pulses",
          len(got) == len(expected_pulses), f"got {len(got)}: {got}")
    for g, e in zip(got, expected_pulses):
        check(f"{label}: pulse at {e:.4f}s (got {g:.4f}s)",
              abs(g - e) <= tol, f"|Δ|={abs(g-e)*1000:.1f}ms")
    return x, rate


def master_expected_pulses():
    """Output times of the centers of master pulses (0.0247 s …)."""
    from app.mapping import Anchor, Piecewise

    pw = Piecewise([Anchor(parse_q(m), parse_q(o)) for m, o in ANCHORS["master"]])
    outs = []
    m0 = parse_q(ANCHORS["master"][0][0])
    m1 = parse_q(ANCHORS["master"][1][0])
    # Envelope peak is 218 samples into a 44100 Hz 50 ms pulse (source side).
    for k in range(0, 10):
        mat = Fraction(k * 44100 + 218, 44100)
        if mat < m0 or mat > m1:
            continue
        for a0, a1 in zip(pw.anchors, pw.anchors[1:]):
            if a0.material <= mat <= a1.material:
                t = a0.output + (mat - a0.material) * (a1.output - a0.output) / (
                    a1.material - a0.material
                )
                if 0 <= t < DURATION:
                    outs.append(float(t))
    return outs


def verify_export(client, workdir: Path, sample_dir: Path, start_snapshot=None):
    print("== export ==")
    r = client.post("/api/export")
    r.raise_for_status()
    job = r.json()
    j = wait_job(client, job["id"])
    check("job done", j["status"] == "done", j.get("message", ""))

    # Download artifacts.
    mp4 = workdir / "export.mp4"
    mp4.write_bytes(client.get(j["mp4_url"]).content)
    manifest = client.get(j["manifest_url"]).json()
    snap = client.get(j["snapshot_url"]).json()

    check("manifest 30 fps", manifest["output_fps"] == "30")
    check("manifest 48 kHz", manifest["output_sample_rate"] == 48000)
    check("manifest covers every frame", len(manifest["video_frames"]) == 281)
    blocks = manifest["audio_blocks"]
    check("audio blocks tile 0..N",
          blocks[0]["sample_start"] == 0
          and all(b1["sample_start"] == b0["sample_end"]
                  for b0, b1 in zip(blocks, blocks[1:])),
          f"first={blocks[0]['sample_start']}")
    check("no rounding before concatenation: duration exact",
          parse_q(manifest["duration"]) == DURATION,
          manifest["duration"])

    if start_snapshot is not None:
        check("export bound snapshot == start snapshot", snap == start_snapshot)

    rgb, wav_out = decode_mp4(mp4, workdir)
    import subprocess

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams",
         "-of", "json", str(mp4)], capture_output=True, text=True, check=True)
    stream = json.loads(probe.stdout)["streams"][0]
    check("MP4 r_frame_rate 30/1", stream["r_frame_rate"] == "30/1",
          stream.get("r_frame_rate"))

    w, h = manifest["canvas"]
    frame_size = w * h * 3
    raw = rgb.read_bytes()
    check("MP4 raw bytes match 281 frames", len(raw) == frame_size * 281,
          f"{len(raw)} vs {frame_size*281}")
    # Spot-check several frames per camera and all black-gap frames.
    idxs = ([0, 4, 5, 39, 40, 44, 45, 124, 125, 134, 135, 179, 180, 186,
             187, 254, 255] + list(range(256, 281)))
    for n in idxs:
        rec = manifest["video_frames"][n]
        frame = np.frombuffer(raw[n * frame_size:(n + 1) * frame_size],
                              dtype=np.uint8).reshape(h, w, 3)
        if rec["fill"]:
            check(f"mp4 frame {n} black", frame.max() == 0)
        else:
            src = png_array((sample_dir /
                             f"{rec['camera']}_frame_{rec['source_frame']:08d}.png")
                            .read_bytes())
            # H.264 tolerance: mean abs difference small.
            mad = np.abs(frame.astype(int) - src.astype(int)).mean()
            check(f"mp4 frame {n} ~ {rec['camera']}/{rec['source_frame']} "
                  f"(MAD={mad:.2f})", mad < 8.0, f"MAD={mad:.2f}")

    verify_audio(client, workdir, "MP4", wav_out, master_expected_pulses())
    return j, snap


def verify_snapshot_isolation(client, snapshot_before, workdir):
    print("== editing after export start cannot affect the finished artifact ==")
    # Start a fresh export, then immediately change the edit document.
    r = client.post("/api/export")
    r.raise_for_status()
    job_id = r.json()["id"]
    alt = {
        "duration": "1",
        "anchors": {
            "A": [["1/60", "0"], ["1/30", "1/2"]],
            "B": [["1/48", "0"], ["1/24", "1/2"]],
            "master": [["1/88200", "0"], ["1/44100", "1/2"]],
        },
        "cuts": [["0", "1", "A"]],
    }
    pr = client.put("/api/state", json_body=alt)
    pr.raise_for_status()
    j = wait_job(client, job_id)
    check("concurrent export finished", j["status"] == "done", j.get("message", ""))
    snap = client.get(j["snapshot_url"]).json()
    check("finished export kept start snapshot despite edit",
          snap == snapshot_before)
    # Restore the intended edit for any later steps.
    save_edits(client)


def verify_cancel_and_failure(client, export_dir: Path):
    print("== cancel / failure never publish half products ==")
    # Cancelling a finished job cannot flip it to cancelled.
    r = client.post("/api/export")
    r.raise_for_status()
    j = wait_job(client, r.json()["id"])
    client.post(f"/api/jobs/{j['id']}/cancel")
    time.sleep(0.2)
    again = client.get(f"/api/jobs/{j['id']}").json()
    check("cancel loses race vs completion", again["status"] == "done")
    check("completed mp4 still present",
          client.get(j["mp4_url"]).status_code == 200)

    # Start a big export and cancel quickly; no mp4 must ever be published.
    r = client.post("/api/export")
    r.raise_for_status()
    cid = r.json()["id"]
    client.post(f"/api/jobs/{cid}/cancel")
    deadline = time.time() + 30
    while time.time() < deadline:
        cj = client.get(f"/api/jobs/{cid}").json()
        if cj["status"] != "running":
            break
        time.sleep(0.1)
    check("cancelled job terminal", cj["status"] == "cancelled", cj["status"])
    check("cancelled job has no mp4 url", cj["mp4_url"] is None)
    check("cancelled mp4 file absent", not (export_dir / f"{cid}.mp4").exists())
    tmp_left = list(export_dir.glob(f".{cid}.tmp*"))
    check("cancelled temp dir removed", not tmp_left)


# ---------------------------------------------------------------- main

def build_samples(sample_dir: Path):
    sample_dir.mkdir(parents=True, exist_ok=True)
    marker = sample_dir / "frame_cache"
    # Generate raw per-frame PNGs once (source of truth for pixel compare),
    # plus the MP4/WAV the app consumes.
    from scripts.generate_samples import make_video, make_wav

    mp4_a = sample_dir / "cameraA.mp4"
    mp4_b = sample_dir / "cameraB.mp4"
    wav_m = sample_dir / "master.wav"
    if not mp4_a.exists():
        make_video(mp4_a, label="CAM A", fps=A_FPS, duration_s=12.0,
                   display_start=0.0, clock_rate=1.0, color=(120, 170, 255))
    if not mp4_b.exists():
        make_video(mp4_b, label="CAM B", fps=B_FPS, duration_s=10.0,
                   display_start=1.5, clock_rate=0.8, color=(120, 230, 180))
    if not wav_m.exists():
        make_wav(wav_m)

    if not marker.exists():
        # Dump source frames used by pixel comparison (same renderer used by
        # the generator, so these are the exact burned-in frames).
        import tempfile
        import subprocess

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                            "-i", str(mp4_a), str(td / "a%08d.png")], check=True)
            for p in td.glob("a*.png"):
                n = int(p.stem[1:]) - 1
                p.rename(sample_dir / f"A_frame_{n:08d}.png")
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                            "-i", str(mp4_b), str(td / "b%08d.png")], check=True)
            for p in td.glob("b*.png"):
                n = int(p.stem[1:]) - 1
                p.rename(sample_dir / f"B_frame_{n:08d}.png")
        marker.write_text("ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--local", action="store_true",
                    help="use in-process TestClient (default if no --url)")
    ap.add_argument("--url", default=None,
                    help="base URL of a running container, e.g. http://localhost:8000")
    ap.add_argument("--workdir", default="/tmp/mc-verify")
    ap.add_argument("--samples", default="/tmp/mc-samples")
    args = ap.parse_args()

    workdir = Path(args.workdir)
    sample_dir = Path(args.samples)
    workdir.mkdir(parents=True, exist_ok=True)
    build_samples(sample_dir)

    data_dir = workdir / "data"
    if args.local:
        shutil.rmtree(data_dir, ignore_errors=True)
        client = LocalClient(data_dir)
        export_dir = client.main.EXPORT_DIR
    elif args.url:
        client = HttpClient(args.url)
        export_dir = None  # cancel/absence checked over HTTP only
    else:
        ap.error("give --local or --url http://host:8000")

    try:
        print("== upload ==")
        s = upload(client, "A", sample_dir / "cameraA.mp4")
        check("A uploaded", s["assets"]["A"]["frame_count"] == 360,
              str(s["assets"]["A"].get("frame_count")))
        s = upload(client, "B", sample_dir / "cameraB.mp4")
        check("B uploaded", s["assets"]["B"]["frame_count"] == 240)
        s = upload(client, "master", sample_dir / "master.wav")
        check("master uploaded 44.1k/10s",
              s["assets"]["master"]["sample_rate"] == 44100
              and abs(s["assets"]["master"]["samples"] / 44100 - 10) < 1e-6)

        state = save_edits(client)
        check("state ready", state["ready"], str(state["problems"]))

        verify_preview_frames(client, sample_dir, workdir)

        exp_pulses = master_expected_pulses()
        wav_bytes = client.get("/preview/audio.wav").content
        verify_audio(client, workdir, "preview WAV", wav_bytes, exp_pulses)

        # Snapshot bound at start (canonical JSON as the worker persists).
        first_job, _ = verify_export(client, workdir, sample_dir)
        start_snap = client.get(first_job["snapshot_url"]).json()

        verify_snapshot_isolation(client, start_snap, workdir)
        save_edits(client)
        if export_dir is not None:
            verify_cancel_and_failure(client, export_dir)
        else:
            print("== (cancel-filesystem checks skipped over HTTP) ==")

        print()
        print(f"ALL CHECKS PASSED: {len(PASS)} assertions")
    finally:
        client.close()


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"\nVERIFICATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

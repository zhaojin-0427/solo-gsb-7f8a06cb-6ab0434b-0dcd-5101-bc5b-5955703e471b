"""FastAPI application: uploads, edit document, shared preview, export jobs."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from . import media
from .db import Database, bump_edit_version
from .jobs import JobManager
from .mapping import ValidationError
from .rationals import OUTPUT_FPS, parse_q, q_to_json
from .renderer import Plan
from .state import (
    ALL_ROLES,
    CAMERA_ROLES,
    MASTER_ROLE,
    compute_canvas,
    load_state,
    snapshot,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("MC_DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
EXTRACT_DIR = DATA_DIR / "assets"
EXPORT_DIR = DATA_DIR / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app.state.db = Database(DATA_DIR / "mc.sqlite3")
    app.state.jobs = JobManager(EXPORT_DIR, DATA_DIR)
    yield


app = FastAPI(title="Multi-Camera Sync Editor", lifespan=lifespan)


# ---------------------------------------------------------------- state I/O

def _current_state(request: Request):
    with request.app.state.db.connect() as conn:
        return load_state(conn, DATA_DIR)


def _state_payload(state) -> dict:
    ready, problems = state.ready_for_export()
    return {
        "duration": q_to_json(state.duration),
        "edit_version": state.edit_version,
        "canvas": list(state.canvas),
        "output_fps": q_to_json(OUTPUT_FPS),
        "output_sample_rate": 48000,
        "ready": ready,
        "problems": problems,
        "errors": state.errors,
        "assets": {
            role: {
                "kind": a.kind,
                "fps": q_to_json(a.fps) if a.fps is not None else None,
                "frame_count": a.frame_count,
                "width": a.width,
                "height": a.height,
                "sample_rate": a.sample_rate,
                "channels": a.channels,
                "samples": a.samples,
                "duration_material_seconds": (
                    a.samples / a.sample_rate if a.kind == "audio" and a.sample_rate
                    else (a.frame_count / float(a.fps) if a.kind == "video" and a.fps and a.frame_count else None)
                ),
                "uploaded_version": a.uploaded_version,
            }
            for role, a in state.assets.items()
        },
        "anchors": {
            role: [[q_to_json(a.material), q_to_json(a.output)] for a in ans]
            for role, ans in state.anchors.items()
        },
        "cuts": [[q_to_json(c.start), q_to_json(c.end), c.camera] for c in state.cuts],
    }


@app.get("/api/state")
def get_state(request: Request):
    return _state_payload(_current_state(request))


@app.put("/api/state")
async def put_state(request: Request):
    body = await request.json()
    try:
        duration = parse_q(body.get("duration", "0"), "duration")
        if duration < 0:
            raise ValidationError("duration must be >= 0")
        anchors_in = body.get("anchors", {})
        parsed_anchors: dict[str, list] = {}
        for role, pairs in anchors_in.items():
            if role not in ALL_ROLES:
                raise ValidationError(f"unknown role {role!r}")
            ans = []
            for i, pair in enumerate(pairs):
                m = parse_q(pair[0], f"{role}.anchors[{i}].material")
                o = parse_q(pair[1], f"{role}.anchors[{i}].output")
                ans.append((m, o))
            mats = [m for m, _ in ans]
            outs = [o for _, o in ans]
            if len(ans) >= 2:
                if any(b <= a for a, b in zip(mats, mats[1:])):
                    raise ValidationError(f"{role}: material times must be strictly increasing")
                if any(b <= a for a, b in zip(outs, outs[1:])):
                    raise ValidationError(f"{role}: output times must be strictly increasing")
            parsed_anchors[role] = ans
        cuts_in = body.get("cuts", [])
        parsed_cuts = []
        from .mapping import Cut, validate_cuts

        for i, c in enumerate(cuts_in):
            s = parse_q(c[0], f"cuts[{i}].start")
            e = parse_q(c[1], f"cuts[{i}].end")
            cam = c[2]
            if cam not in CAMERA_ROLES:
                raise ValidationError(f"cuts[{i}]: camera must be A or B")
            parsed_cuts.append(Cut(s, e, cam))
        validate_cuts(parsed_cuts, duration)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    with request.app.state.db.connect() as conn:
        conn.execute("UPDATE project SET duration=?", (str(duration),))
        conn.execute("DELETE FROM anchors")
        for role, ans in parsed_anchors.items():
            for i, (m, o) in enumerate(ans):
                conn.execute(
                    "INSERT INTO anchors(role, idx, material, output) VALUES (?,?,?,?)",
                    (role, i, str(m), str(o)),
                )
        conn.execute("DELETE FROM cuts")
        for i, cut in enumerate(parsed_cuts):
            conn.execute(
                "INSERT INTO cuts(idx, start, finish, camera) VALUES (?,?,?,?)",
                (i, str(cut.start), str(cut.end), cut.camera),
            )
        version = bump_edit_version(conn)
    state = _current_state(request)
    payload = _state_payload(state)
    payload["edit_version"] = version
    return payload


# ----------------------------------------------------------------- uploads

@app.post("/api/upload")
async def upload(
    request: Request,
    role: str = Form(...),
    file: UploadFile = File(...),
):
    if role not in ALL_ROLES:
        raise HTTPException(status_code=400, detail="role must be A, B or master")

    with request.app.state.db.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(uploaded_version), 0) + 1 AS v FROM assets WHERE role=?",
            (role,),
        ).fetchone()
        version = int(row["v"])

    role_dir = EXTRACT_DIR / f"{role}-v{version}"
    orig_dir = UPLOAD_DIR / f"{role}-v{version}"
    role_dir.mkdir(parents=True, exist_ok=True)
    orig_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "").suffix or (".wav" if role == MASTER_ROLE else ".mp4")
    orig_path = orig_dir / f"original{suffix}"
    with open(orig_path, "wb") as out:
        while chunk := await file.read(1 << 20):
            out.write(chunk)

    try:
        if role == MASTER_ROLE:
            if suffix.lower() not in (".wav",):
                raise HTTPException(status_code=400, detail="master track must be WAV")
            try:
                info = media.probe_audio(orig_path)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=f"invalid WAV: {exc}")
            pcm_path = role_dir / "master.f32le"
            _data, rate, channels, samples = media.decode_wav(orig_path, pcm_path)
            with request.app.state.db.connect() as conn:
                conn.execute("DELETE FROM assets WHERE role=?", (role,))
                conn.execute(
                    """INSERT INTO assets(role, kind, path, sample_rate, channels,
                       samples, pcm_path, uploaded_version)
                       VALUES (?, 'audio', ?, ?, ?, ?, ?, ?)""",
                    (role, str(orig_path), rate, channels, samples,
                     str(pcm_path), version),
                )
                bump_edit_version(conn)
        else:
            try:
                info = media.probe_video(orig_path)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=f"invalid video: {exc}")
            # Canvas = max even native dimensions over all videos.
            with request.app.state.db.connect() as conn:
                prior_videos = {
                    r["role"]: r
                    for r in conn.execute(
                        "SELECT role, width, height, fps, path FROM assets WHERE kind='video'"
                    )
                }
            dims = [(int(info["width"]), int(info["height"]))]
            for r in prior_videos.values():
                dims.append((int(r["width"]), int(r["height"])))
            canvas_w = max(d[0] for d in dims)
            canvas_h = max(d[1] for d in dims)
            canvas = (
                canvas_w if canvas_w % 2 == 0 else canvas_w - 1,
                canvas_h if canvas_h % 2 == 0 else canvas_h - 1,
            )
            frames_dir = role_dir / "frames"
            count = media.extract_frames(orig_path, frames_dir, info["fps"], canvas)

            # Re-extract any prior video onto the (possibly grown) canvas, into
            # a fresh directory so running snapshots keep their old frames.
            reextract = {}
            for prole, prow in prior_videos.items():
                if prole == role:
                    continue
                new_dir = EXTRACT_DIR / f"{prole}-re-v{version}"
                c = media.extract_frames(
                    Path(prow["path"]), new_dir, parse_q(prow["fps"], "fps"), canvas
                )
                reextract[prole] = (str(new_dir), c)

            with request.app.state.db.connect() as conn:
                conn.execute("DELETE FROM assets WHERE role=?", (role,))
                conn.execute(
                    """INSERT INTO assets(role, kind, path, fps, frames_dir,
                       frame_count, width, height, uploaded_version)
                       VALUES (?, 'video', ?, ?, ?, ?, ?, ?, ?)""",
                    (role, str(orig_path), str(info["fps"]), str(frames_dir),
                     count, info["width"], info["height"], version),
                )
                for prole, (new_dir, c) in reextract.items():
                    conn.execute(
                        "UPDATE assets SET frames_dir=?, frame_count=? WHERE role=?",
                        (new_dir, c, prole),
                    )
                bump_edit_version(conn)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))

    return _state_payload(_current_state(request))


# ----------------------------------------------------------------- preview

def _build_plan(request: Request) -> Plan:
    state = _current_state(request)
    if state.canvas == (0, 0):
        raise HTTPException(status_code=503, detail="upload a camera video first")
    return Plan(snapshot(state))


@app.get("/preview/frame/{n}.png")
def preview_frame(request: Request, n: int):
    state = _current_state(request)
    if state.canvas == (0, 0):
        raise HTTPException(status_code=503, detail="upload a camera video first")
    plan = Plan(snapshot(state))
    rec = plan.frame_record(n)
    if rec is None:
        raise HTTPException(status_code=404, detail="frame out of range")
    png, _rec = plan.frame_png(n)
    headers = {
        "Cache-Control": "no-store",
        "X-Frame-Index": str(rec.index),
        "X-Output-Time": q_to_json(rec.output_time),
        "X-Camera": rec.camera or "",
        "X-Source-Frame": "" if rec.source_frame is None else str(rec.source_frame),
        "X-Material-Time": "" if rec.material_time is None else q_to_json(rec.material_time),
        "X-Fill": rec.reason if rec.reason != "ok" else "",
    }
    return Response(content=png, media_type="image/png", headers=headers)


@app.get("/api/frame-info")
def frame_info(request: Request, n: int):
    plan = _build_plan(request)
    rec = plan.frame_record(n)
    if rec is None:
        raise HTTPException(status_code=404, detail="frame out of range")
    return {
        "frame": rec.index,
        "output_time": q_to_json(rec.output_time),
        "camera": rec.camera,
        "material_time": q_to_json(rec.material_time) if rec.material_time is not None else None,
        "source_fps": q_to_json(rec.source_fps) if rec.source_fps is not None else None,
        "source_frame": rec.source_frame,
        "fill": rec.reason if rec.reason != "ok" else None,
    }


@app.get("/preview/audio.wav")
def preview_audio(request: Request):
    plan = _build_plan(request)
    if "master" not in plan.assets:
        raise HTTPException(status_code=503, detail="upload the master WAV first")
    data = plan.render_wav()
    return Response(
        content=data,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )


# ------------------------------------------------------------------ export

@app.post("/api/export")
def start_export(request: Request):
    state = _current_state(request)
    ready, problems = state.ready_for_export()
    if not ready:
        raise HTTPException(status_code=400, detail={"ready": False, "problems": problems})
    snap = snapshot(state)
    plan = Plan(snap)
    job = request.app.state.jobs.start(plan, snap)
    return job.public()


@app.get("/api/jobs/{job_id}")
def job_status(request: Request, job_id: str):
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return job.public()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: str):
    job = request.app.state.jobs.request_cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return job.public()


@app.get("/api/jobs")
def list_jobs(request: Request):
    return [j.public() for j in reversed(request.app.state.jobs.jobs.values())]


# ------------------------------------------------------------------ static

@app.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse(url="/web/index.html")


app.mount("/web", StaticFiles(directory=str(BASE_DIR / "web")), name="web")
app.mount("/exports", StaticFiles(directory=str(EXPORT_DIR)), name="exports")

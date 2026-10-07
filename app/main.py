"""FastAPI application: asset upload, edit state, exact preview, export."""
from __future__ import annotations

import json
import os
import secrets
import threading
from fractions import Fraction
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db as db_mod
from . import exporter
from . import media as media_mod
from .render import render_png_preview
from .time import FPS, SAMPLE_RATE, Q, frac_str
from .timeline import EditError, build_edit_state

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
ASSET_DIR = DATA_DIR / "assets"
EXPORT_DIR = DATA_DIR / "exports"
TMP_DIR = DATA_DIR / "tmp"
DB_PATH = DATA_DIR / "editor.db"
STATIC_DIR = BASE_DIR / "static"
PROJECT_ID = 1

for d in (ASSET_DIR, EXPORT_DIR, TMP_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Local Multi-Cam Sync Editor")
conn = db_mod.connect(DB_PATH)
db_mod.init(conn)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _asset_public(row: dict) -> dict:
    return {
        "id": row["id"], "kind": row["kind"], "filename": row["filename"],
        "width": row["width"], "height": row["height"],
        "fps": (frac_str(Fraction(row["fps_num"], row["fps_den"]))
                if row["fps_num"] else None),
        "nframes": row["nframes"],
        "channels": row["channels"],
        "in_sample_rate": row["in_sample_rate"],
        "nsamples": row["nsamples"],
        "material0": row["material0"],
        "clock": frac_str(Fraction(row["clock_num"], row["clock_den"])),
        "material_span": row["material_span"],
    }


def _load_asset_rows() -> dict[int, dict]:
    return {a["id"]: a for a in db_mod.list_assets(conn, PROJECT_ID)}


def _current_state():
    """Build validated EditState from the LIVE edit (preview uses it too)."""
    payload = db_mod.load_edit(conn, PROJECT_ID)
    if payload is None:
        raise HTTPException(400, "no edit yet - set anchors, cuts and duration")
    rows = _load_asset_rows()
    try:
        return build_edit_state(payload, rows), payload
    except EditError as e:
        raise HTTPException(400, str(e)) from e


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

@app.get("/api/state")
def get_state():
    assets = [_asset_public(a) for a in db_mod.list_assets(conn, PROJECT_ID)]
    edit = db_mod.load_edit(conn, PROJECT_ID)
    exports = db_mod.list_exports(conn, PROJECT_ID)
    info = {"assets": assets, "edit": edit, "exports": exports,
            "fps": 30, "sample_rate": 48000}
    try:
        state, _ = _current_state() if edit else (None, None)
        if state is not None:
            info["output"] = {
                "width": state.width, "height": state.height,
                "frames": int(state.duration * FPS),
                "samples": int(state.duration * SAMPLE_RATE),
                "duration": frac_str(state.duration),
            }
    except HTTPException:
        pass
    return info


@app.post("/api/assets")
async def upload_asset(file: UploadFile = File(...), kind: str = Form("video")):
    if kind not in ("video", "audio"):
        raise HTTPException(400, "kind must be video or audio")
    token = secrets.token_hex(8)
    suffix = Path(file.filename or "").suffix.lower()
    dest = ASSET_DIR / f"{token}{suffix}"
    with dest.open("wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    try:
        if kind == "video":
            p = media_mod.probe_video(dest)
            meta = {
                "width": p["width"], "height": p["height"],
                "fps_num": p["fps"].numerator, "fps_den": p["fps"].denominator,
                "nframes": p["nframes"],
                "material_span": str(p["duration"]),
            }
        else:
            p = media_mod.probe_audio(dest)
            meta = {
                "channels": p["channels"],
                "in_sample_rate": p["in_sample_rate"],
                "nsamples": p["nsamples"],
                "material_span": str(p["duration"]),
            }
    except Exception as e:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"unsupported/corrupt media: {e}") from e

    aid = db_mod.create_asset(conn, PROJECT_ID, kind, file.filename,
                              str(dest), meta)
    return _asset_public(db_mod.get_asset(conn, aid))


class ClockIn(BaseModel):
    material0: str | int | float = "0"
    clock: str | int | float = 1


@app.put("/api/assets/{asset_id}/clock")
def set_asset_clock(asset_id: int, body: ClockIn):
    row = db_mod.get_asset(conn, asset_id)
    if row is None:
        raise HTTPException(404, "asset not found")
    try:
        m0 = Q(body.material0, "material0", allow_negative=True)
        clock = Q(body.clock, "clock")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if clock <= 0:
        raise HTTPException(400, "clock speed must be > 0")
    db_mod.update_asset_clock(conn, asset_id, str(m0),
                              clock.numerator, clock.denominator)
    return _asset_public(db_mod.get_asset(conn, asset_id))


class EditIn(BaseModel):
    duration: str | int | float
    anchors: dict[str, list[dict]]
    cuts: list[dict] = []


@app.put("/api/edit")
def save_edit(body: EditIn):
    payload = body.model_dump()
    try:
        state = build_edit_state(payload, _load_asset_rows())
    except EditError as e:
        raise HTTPException(400, str(e)) from e
    db_mod.save_edit(conn, PROJECT_ID, payload)
    return {
        "ok": True,
        "frames": int(state.duration * FPS),
        "samples": int(state.duration * SAMPLE_RATE),
        "width": state.width, "height": state.height,
    }


@app.get("/api/preview/{frame}")
def preview(frame: int, rec: int = 0):
    """Single rendered output frame (exact same lookup as the export)."""
    state, _ = _current_state()
    nframes = int(state.duration * FPS)
    if frame < 0 or frame >= nframes:
        raise HTTPException(404, "frame out of range")
    png, record = render_png_preview(state, frame)
    if rec:
        return JSONResponse(record)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/export/start")
def export_start():
    state, payload = _current_state()
    snapshot = db_mod.make_snapshot(conn, PROJECT_ID, payload)
    job_id = db_mod.create_export(conn, PROJECT_ID, snapshot)
    t = threading.Thread(
        target=exporter.run_export,
        args=(str(DB_PATH), job_id, str(EXPORT_DIR), str(TMP_DIR)),
        daemon=True,
    )
    t.start()
    return {"job_id": job_id, "status": "running"}


@app.post("/api/export/{job_id}/cancel")
def export_cancel(job_id: str):
    job = db_mod.get_export(conn, job_id)
    if job is None:
        raise HTTPException(404, "no such export")
    exporter.request_cancel(job_id)
    if job["status"] == "running":
        # Try to win the terminal state; the render may still win 'done'.
        db_mod.claim_terminal(conn, job_id, "canceled")
    return {"job_id": job_id, "status":
            db_mod.get_export(conn, job_id)["status"]}


@app.get("/api/export/{job_id}")
def export_status(job_id: str):
    job = db_mod.get_export(conn, job_id)
    if job is None:
        raise HTTPException(404, "no such export")
    return {
        "job_id": job_id, "status": job["status"],
        "error": job["error"],
        "download_url": f"/api/export/{job_id}/mp4" if job["output_path"] else None,
        "mapping_url": f"/api/export/{job_id}/mapping" if job["mapping_path"] else None,
    }


@app.get("/api/export/{job_id}/mp4")
def export_mp4(job_id: str):
    job = db_mod.get_export(conn, job_id)
    if not job or job["status"] != "done" or not job["output_path"]:
        raise HTTPException(404, "export not finished")
    return FileResponse(job["output_path"], media_type="video/mp4",
                        filename=f"edit-{job_id[:8]}.mp4")


@app.get("/api/export/{job_id}/mapping")
def export_mapping(job_id: str):
    job = db_mod.get_export(conn, job_id)
    if not job or job["status"] != "done" or not job["mapping_path"]:
        raise HTTPException(404, "export not finished")
    return FileResponse(job["mapping_path"], media_type="application/json",
                        filename=f"edit-{job_id[:8]}.mapping.json")


@app.get("/healthz")
def healthz():
    return {"ok": True}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

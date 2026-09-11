"""Voxelo 4D prototype API — upload a clip, queue a job, browse scans, serve the web app.

Contract: see ../API.md (v1). The web client under app/web is one consumer; an iOS app or a
separate SaaS front end can consume the same routes. Everything a client needs to render a scan is
in the scan's manifest.json plus the static files next to it.

Single process, single GPU: a background thread drains the job queue one job at a time by running
``python -m app.pipeline.run_job <id>`` as a subprocess (so a crashing job never takes the API down).

Run:  uvicorn app.api.main:app --host 0.0.0.0 --port 8000     (see scripts/serve.sh)
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import aiofiles
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.pipeline.presets import get_preset, public_presets

REPO = Path(os.environ.get("FDA_REPO", Path(__file__).resolve().parents[2]))
APPDATA = Path(os.environ.get("FDA_APPDATA", "/workspace/fdanyone-app"))
MODELS = Path(os.environ.get("FDA_MODELS", "/workspace/models"))
WEB = REPO / "app" / "web"
UPLOADS = APPDATA / "uploads"
JOBS = APPDATA / "jobs"
SCANS = APPDATA / "scans"
for d in (UPLOADS, JOBS, SCANS):
    d.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = int(os.environ.get("FDA_MAX_UPLOAD_MB", "600")) * 1024 * 1024
ALLOWED_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
ID_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

app = FastAPI(title="Voxelo 4D prototype (4DAnyone)", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"])


def new_id(n: int = 10) -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(n))


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def job_summary(job_id: str) -> dict | None:
    job = read_json(JOBS / job_id / "job.json")
    state = read_json(JOBS / job_id / "state.json") or {}
    if not job:
        return None
    return {
        "id": job_id,
        "title": job.get("title"),
        "preset": job.get("preset"),
        "original_filename": job.get("original_filename"),
        "created": job.get("created"),
        "status": state.get("status", "queued"),
        "stage": state.get("stage", "queued"),
        "progress": state.get("progress", 0.0),
        "message": state.get("message", ""),
        "error": state.get("error"),
        "started": state.get("started"),
        "finished": state.get("finished"),
        "updated": state.get("updated"),
        "scan_id": state.get("scan_id") or (job.get("scan_id") if state.get("status") == "done" else None),
        "eta_minutes": get_preset(job.get("preset")).get("eta_minutes") if job.get("preset") in ("fast", "standard", "wide", "full") else None,
    }


def list_jobs(limit: int = 40) -> list[dict]:
    items = []
    for d in JOBS.iterdir():
        if d.is_dir() and (d / "job.json").is_file():
            s = job_summary(d.name)
            if s:
                items.append(s)
    items.sort(key=lambda s: s.get("created") or 0, reverse=True)
    return items[:limit]


def scan_summary(manifest: dict) -> dict:
    seq = manifest.get("sequence", {})
    stats = manifest.get("stats", {})
    return {
        "id": manifest["id"],
        "title": manifest.get("title"),
        "created": manifest.get("created"),
        "preset": (manifest.get("preset") or {}).get("name"),
        "views": (manifest.get("preset") or {}).get("views_per_layer", 0) * len((manifest.get("preset") or {}).get("layer_pitches", [1])),
        "frames": seq.get("count"),
        "fps": seq.get("fps"),
        "duration_seconds": seq.get("duration_seconds"),
        "gaussians_per_frame": stats.get("gaussians_per_frame"),
        "bytes_total": stats.get("bytes_total"),
        "poster": f"/scans/{manifest['id']}/poster.jpg",
        "preview": f"/scans/{manifest['id']}/preview.mp4",
        "url": f"/view/{manifest['id']}",
    }


def list_scans() -> list[dict]:
    items = []
    for d in SCANS.iterdir():
        if d.is_dir() and not d.name.startswith(".") and (d / "manifest.json").is_file():
            m = read_json(d / "manifest.json")
            if m:
                items.append(scan_summary(m))
    items.sort(key=lambda s: s.get("created") or "", reverse=True)
    return items


# ── worker: one job at a time, as a subprocess ────────────────────────────────
_worker_state = {"current": None, "started": None}


def next_queued() -> str | None:
    queued = []
    for d in JOBS.iterdir():
        if not d.is_dir():
            continue
        state = read_json(d / "state.json") or {}
        if state.get("status") == "queued":
            queued.append((state.get("created") or 0, d.name))
    queued.sort()
    return queued[0][1] if queued else None


def worker_loop() -> None:
    # jobs left 'running' by a previous API process are re-queued (run_job reuses finished stages)
    for d in JOBS.iterdir():
        state_path = d / "state.json"
        state = read_json(state_path) or {}
        if state.get("status") in ("running", "starting"):
            state.update(status="queued", stage="queued", message="re-queued after API restart")
            write_json_atomic(state_path, state)
    while True:
        job_id = next_queued()
        if not job_id:
            time.sleep(2)
            continue
        state_path = JOBS / job_id / "state.json"
        state = read_json(state_path) or {}
        state.update(status="starting", stage="starting", message="worker picked up the job", updated=time.time())
        write_json_atomic(state_path, state)
        _worker_state.update(current=job_id, started=time.time())
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("XDG_RUNTIME_DIR", "/tmp")
        with (JOBS / job_id / "worker.log").open("a") as fh:
            # stdin must NOT be a tty: 4DAnyone's SMPL-X installer prompts interactively when it sees one
            code = subprocess.call([sys.executable, "-m", "app.pipeline.run_job", job_id], cwd=str(REPO), env=env, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT)
        state = read_json(state_path) or {}
        if code != 0 and state.get("status") != "failed":
            state.update(status="failed", error=f"runner exited with code {code}", finished=time.time())
            write_json_atomic(state_path, state)
        _worker_state.update(current=None, started=None)


@app.on_event("startup")
def _start_worker() -> None:
    if os.environ.get("FDA_NO_WORKER") != "1":
        threading.Thread(target=worker_loop, name="job-worker", daemon=True).start()


# ── API v1 ────────────────────────────────────────────────────────────────────
def _gpu_name() -> str | None:
    try:
        return subprocess.check_output(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], text=True, timeout=5).strip().splitlines()[0]
    except Exception:  # noqa: BLE001
        return None


_GPU = _gpu_name()


@app.get("/api/v1/health")
def health() -> dict:
    smplx = (MODELS / "body_models" / "smplx" / "SMPLX_NEUTRAL.npz").is_file()
    checkpoint = (MODELS / "4danyone" / "model.safetensors").is_file()
    spirula = Path(os.environ.get("FDA_SPIRULA", "/workspace/spirula/spirula")).is_file()
    queued = sum(1 for d in JOBS.iterdir() if (read_json(d / "state.json") or {}).get("status") == "queued")
    return {
        "ok": True,
        "gpu": _GPU,
        "ready": smplx and checkpoint and spirula,
        "smplx_installed": smplx,
        "checkpoint_installed": checkpoint,
        "spirula_installed": spirula,
        "queue": {"queued": queued, "running": _worker_state["current"], "running_since": _worker_state["started"]},
        "limits": {"max_upload_bytes": MAX_UPLOAD_BYTES, "extensions": sorted(ALLOWED_EXT)},
        "time": time.time(),
    }


@app.get("/api/v1/presets")
def presets() -> list[dict]:
    return public_presets()


@app.post("/api/v1/jobs", status_code=201)
async def create_job(video: UploadFile = File(...), preset: str = Form("standard"), title: str = Form("")) -> dict:
    try:
        preset_def = get_preset(preset)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from None
    ext = Path(video.filename or "clip.mp4").suffix.lower() or ".mp4"
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"unsupported file type {ext}; use one of {sorted(ALLOWED_EXT)}")
    job_id = new_id()
    job_dir = JOBS / job_id
    upload_dir = UPLOADS / job_id
    job_dir.mkdir(parents=True)
    upload_dir.mkdir(parents=True)
    dest = upload_dir / f"source{ext}"
    size = 0
    async with aiofiles.open(dest, "wb") as out:
        while True:
            chunk = await video.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                await out.close()
                shutil.rmtree(upload_dir, ignore_errors=True)
                shutil.rmtree(job_dir, ignore_errors=True)
                raise HTTPException(413, f"upload larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
            await out.write(chunk)
    if size == 0:
        shutil.rmtree(upload_dir, ignore_errors=True)
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, "empty upload")
    clean_title = (title or "").strip()[:80] or Path(video.filename or "Untitled").stem[:80]
    now = time.time()
    write_json_atomic(job_dir / "job.json", {
        "id": job_id, "scan_id": job_id, "video": str(dest), "original_filename": video.filename,
        "bytes": size, "preset": preset_def["name"], "title": clean_title, "created": now,
    })
    write_json_atomic(job_dir / "state.json", {"status": "queued", "stage": "queued", "progress": 0.0, "message": "waiting for the GPU", "created": now, "updated": now})
    return job_summary(job_id) or {"id": job_id, "status": "queued"}


@app.get("/api/v1/jobs")
def jobs() -> list[dict]:
    return list_jobs()


@app.get("/api/v1/jobs/{job_id}")
def job(job_id: str) -> dict:
    s = job_summary(job_id)
    if not s:
        raise HTTPException(404, "no such job")
    log_path = JOBS / job_id / "log.txt"
    tail: list[str] = []
    if log_path.is_file():
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-40:]
    s["log_tail"] = [line.rstrip() for line in tail]
    return s


@app.get("/api/v1/jobs/{job_id}/log", response_class=PlainTextResponse)
def job_log(job_id: str) -> str:
    log_path = JOBS / job_id / "log.txt"
    if not log_path.is_file():
        raise HTTPException(404, "no log yet")
    return log_path.read_text(encoding="utf-8", errors="replace")


@app.get("/api/v1/scans")
def scans() -> list[dict]:
    return list_scans()


@app.get("/api/v1/scans/{scan_id}")
def scan(scan_id: str) -> dict:
    m = read_json(SCANS / scan_id / "manifest.json")
    if not m:
        raise HTTPException(404, "no such scan")
    m["base_url"] = f"/scans/{scan_id}/"
    return m


# ── static: scans + web client ────────────────────────────────────────────────
class CachedStaticFiles(StaticFiles):
    """Immutable scan assets get long cache headers (frames never change once published)."""

    async def get_response(self, path, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        if response.status_code == 200 and (path.endswith(".sog") or path.endswith(".jpg") or path.endswith(".mp4")):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


app.mount("/scans", CachedStaticFiles(directory=str(SCANS)), name="scans")
app.mount("/assets", StaticFiles(directory=str(WEB / "assets")), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.get("/view/{scan_id}")
def view(scan_id: str) -> FileResponse:
    if not (SCANS / scan_id / "manifest.json").is_file():
        return FileResponse(WEB / "view.html", status_code=404)
    return FileResponse(WEB / "view.html")


@app.get("/manifest.webmanifest")
def webmanifest() -> JSONResponse:
    return JSONResponse({"name": "Voxelo 4D", "short_name": "Voxelo 4D", "start_url": "/", "display": "standalone", "background_color": "#eaeae0", "theme_color": "#0a0a08"})


@app.exception_handler(404)
async def not_found(request: Request, exc):  # noqa: ANN001
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "not found"}, status_code=404)
    return FileResponse(WEB / "index.html", status_code=404)

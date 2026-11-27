"""Private lab website: upload CT scans, see the 3 ROIs and HU values, download spreadsheets."""
from __future__ import annotations

import io
import json
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .. import __version__
from ..config import Settings
from ..pipeline import flatten
from ..scanio import safe_name
from . import auth
from .jobs import Store, Worker

HERE = Path(__file__).parent
MAX_FILES_PER_REQUEST = 5000


def _safe_rel(name: str) -> Path:
    parts = [p for p in re.split(r"[\\/]+", name or "") if p not in ("", ".", "..")]
    parts = [safe_name(p.replace(":", "_"), "f") for p in parts]
    return Path(*parts) if parts else Path("file")


def _summary(row: dict) -> dict:
    res = json.loads(row["result_json"]) if row.get("result_json") else {}
    rois = {r["key"]: r for r in res.get("rois", [])}
    out = {"id": row["id"], "case_id": row["case_id"], "status": row["status"],
           "step": row.get("current_step") or "", "seconds": row.get("seconds"),
           "started_at": row.get("started_at"),
           "error": res.get("error"), "flags": res.get("flags", []),
           "liver_mean": res.get("liver_mean_hu_3roi"), "has_qa": False,
           "spleen_mean": res.get("spleen_mean_hu"),
           "l_minus_s": res.get("liver_minus_spleen_hu"), "l_s_ratio": res.get("liver_spleen_ratio"),
           "series": (res.get("input") or {}).get("series_description"),
           "slice_mm": (res.get("input") or {}).get("slice_thickness_mm")}
    for k in ("LL", "RL1", "RL2", "SPL"):
        r = rois.get(k) or {}
        out[k] = {f: r.get(f) for f in ("placed", "mean_hu", "sd_hu", "min_hu", "max_hu", "area_cm2",
                                        "diameter_mm", "z_mm", "dicom_instance", "segment_at_center",
                                        "side_of_ligament", "side_ok")}
    return out


def create_app(data: Path, keep_uploads: bool = False, start_worker: bool = True) -> FastAPI:
    data = Path(data)
    store = Store(data)
    worker = Worker(store, keep_uploads=keep_uploads)

    @asynccontextmanager
    async def lifespan(_app):
        if start_worker and not worker.is_alive():
            worker.start()
        yield

    app = FastAPI(title="Liver HU", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store, app.state.worker = store, worker
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    security = HTTPBasic(realm="Liver HU lab website")

    def user(creds: HTTPBasicCredentials = Depends(security)) -> str:
        if not auth.check_password(data, creds.password):
            raise HTTPException(401, "Wrong password", headers={"WWW-Authenticate": 'Basic realm="Liver HU"'})
        return (creds.username or "lab").strip()[:60] or "lab"

    def page(request: Request, name: str, **ctx):
        return templates.TemplateResponse(request, name, {"version": __version__, "defaults": Settings(), **ctx})

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, u: str = Depends(user)):
        return page(request, "index.html", user=u)

    @app.get("/jobs/{jid}", response_class=HTMLResponse)
    def job_page(request: Request, jid: int, u: str = Depends(user)):
        job = store.q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
        if not job:
            raise HTTPException(404, "No such upload")
        return page(request, "job.html", user=u, job=job)

    @app.get("/about", response_class=HTMLResponse)
    def about(request: Request, u: str = Depends(user)):
        return page(request, "about.html", user=u)

    # ---------- API ----------
    @app.post("/api/jobs")
    async def create_job(request: Request, u: str = Depends(user)):
        form = await request.form()
        d = Settings()
        try:
            s = Settings(
                roi_diameter_mm=min(max(float(form.get("roi_diameter_mm") or d.roi_diameter_mm), 5.0), 40.0),
                edge_margin_mm=min(max(float(form.get("edge_margin_mm") or d.edge_margin_mm), 0.0), 20.0),
                ligament_margin_mm=min(max(float(form.get("ligament_margin_mm") or d.ligament_margin_mm), 0.0), 40.0),
                preferred_slice_mm=min(max(float(form.get("preferred_slice_mm") or d.preferred_slice_mm), 0.5), 10.0),
                save_segmentation=str(form.get("save_segmentation", "")).lower() in ("1", "true", "on"))
        except ValueError:
            raise HTTPException(400, "Settings must be numbers")
        label = safe_name(str(form.get("label") or ""), "")[:40]
        ident = str(form.get("include_identifiers", "")).lower() in ("1", "true", "on")
        jid = store.create_job(label, u, s, ident)
        return {"job_id": jid}

    @app.post("/api/jobs/{jid}/files")
    async def add_files(request: Request, jid: int, u: str = Depends(user)):
        job = store.q("SELECT status FROM jobs WHERE id=?", (jid,), one=True)
        if not job or job["status"] != "uploading":
            raise HTTPException(409, "This upload is closed")
        form = await request.form(max_files=MAX_FILES_PER_REQUEST, max_fields=MAX_FILES_PER_REQUEST + 50)
        root = store.upload_dir(jid).resolve()
        saved = 0
        for _, f in form.multi_items():
            if not hasattr(f, "filename") or not f.filename:
                continue
            dest = (root / _safe_rel(f.filename)).resolve()
            if root not in dest.parents:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            n = 2
            base = dest
            while dest.exists():          # never overwrite a file with the same name
                dest = base.with_name(f"{base.name}__{n}")
                n += 1

            def _copy(src=f.file, dst=dest):
                with open(dst, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
            await run_in_threadpool(_copy)
            saved += 1
        store.x("UPDATE jobs SET n_files=n_files+? WHERE id=?", (saved, jid))
        return {"saved": saved}

    @app.post("/api/jobs/{jid}/start")
    def start_job(jid: int, u: str = Depends(user)):
        job = store.q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
        if not job:
            raise HTTPException(404)
        if job["status"] == "uploading":
            store.x("UPDATE jobs SET status='queued' WHERE id=?", (jid,))
            worker.wake.set()
        return {"ok": True}

    @app.post("/api/jobs/{jid}/delete")
    def delete_job(jid: int, u: str = Depends(user)):
        job = store.q("SELECT status FROM jobs WHERE id=?", (jid,), one=True)
        if not job:
            raise HTTPException(404)
        if job["status"] == "running":
            store.x("UPDATE jobs SET status='cancelled' WHERE id=?", (jid,))
            if worker.current_proc:
                worker.current_proc.kill()
        store.delete_job(jid)
        return {"ok": True}

    @app.get("/api/jobs")
    def list_jobs(u: str = Depends(user)):
        jobs = store.q("SELECT id, label, submitted_by, created_at, status, n_cases, n_done, n_files, message "
                       "FROM jobs ORDER BY id DESC LIMIT 200")
        avg = store.q("SELECT AVG(seconds) AS a FROM cases WHERE status IN ('ok','partial')", one=True)["a"]
        queued = store.q("SELECT COUNT(*) AS n FROM cases WHERE status IN ('queued','running')", one=True)["n"]
        return {"jobs": jobs, "avg_seconds_per_case": avg, "cases_waiting": queued}

    @app.get("/api/jobs/{jid}")
    def job_detail(jid: int, u: str = Depends(user)):
        job = store.q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
        if not job:
            raise HTTPException(404)
        rows = store.q("SELECT * FROM cases WHERE job_id=? ORDER BY id", (jid,))
        cases = [_summary(r) for r in rows]
        for c, r in zip(cases, rows):
            c["has_qa"] = (store.case_dir(jid, r["id"]) / "qa.png").exists()
        ahead = store.q("SELECT COUNT(*) AS n FROM cases WHERE status IN ('queued','running') AND job_id<?",
                        (jid,), one=True)["n"]
        avg = store.q("SELECT AVG(seconds) AS a FROM cases WHERE status IN ('ok','partial')", one=True)["a"]
        job.pop("settings_json", None)
        return {"job": job, "cases": cases, "cases_ahead": ahead, "avg_seconds_per_case": avg}

    def _case_row(case_id: int) -> dict:
        row = store.q("SELECT * FROM cases WHERE id=?", (case_id,), one=True)
        if not row:
            raise HTTPException(404)
        return row

    @app.get("/cases/{case_id}/qa.png")
    def qa_png(case_id: int, u: str = Depends(user)):
        row = _case_row(case_id)
        p = store.case_dir(row["job_id"], row["id"]) / "qa.png"
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/png")

    @app.get("/cases/{case_id}/result.json")
    def result_json(case_id: int, u: str = Depends(user)):
        row = _case_row(case_id)
        return JSONResponse(json.loads(row["result_json"] or "{}"))

    @app.get("/cases/{case_id}/log.txt")
    def case_log(case_id: int, u: str = Depends(user)):
        row = _case_row(case_id)
        p = store.case_dir(row["job_id"], row["id"]) / "log.txt"
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="text/plain")

    def _rows(job: int | None):
        if job:
            rows = store.q("SELECT result_json FROM cases WHERE job_id=? AND result_json IS NOT NULL ORDER BY id", (job,))
        else:
            rows = store.q("SELECT result_json FROM cases WHERE result_json IS NOT NULL ORDER BY id")
        return [flatten(json.loads(r["result_json"])) for r in rows]

    @app.get("/download/results.{ext}")
    def download(ext: str, job: int | None = None, u: str = Depends(user)):
        import pandas as pd
        df = pd.DataFrame(_rows(job))
        name = f"liver_hu_results{'_upload' + str(job) if job else '_all'}"
        if ext == "csv":
            return StreamingResponse(io.BytesIO(df.to_csv(index=False).encode()), media_type="text/csv",
                                     headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
        if ext == "xlsx":
            buf = io.BytesIO()
            df.to_excel(buf, index=False)
            buf.seek(0)
            return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                     headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})
        raise HTTPException(404)

    return app

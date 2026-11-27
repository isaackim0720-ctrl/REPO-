"""Private lab website: upload CT scans, see the 3 ROIs and HU values, download spreadsheets."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import math
import re
import secrets
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import numpy as np
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders

from .. import __version__
from ..config import Settings
from ..pipeline import flatten
from ..scanio import safe_name
from . import auth
from .jobs import FINISHED, Intake, Store, Worker

HERE = Path(__file__).parent
MAX_FILES_PER_REQUEST = 5000
MIN_FREE_BYTES = 2 * 1024 ** 3      # refuse uploads when the computer is nearly out of disk space


def _on(v, default=False) -> bool:
    if v is None or v == "":
        return default
    return str(v).lower() in ("1", "true", "on", "yes")


def _num(form, key: str, default: float, lo: float, hi: float) -> float:
    v = form.get(key)
    try:
        f = float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        raise HTTPException(400, f"{key} must be a number")
    return min(max(f, lo), hi)


def _measure_settings(form) -> dict:
    """Settings that apply when a study is measured (from the upload form or the library)."""
    d = Settings()
    return {"roi_diameter_mm": _num(form, "roi_diameter_mm", d.roi_diameter_mm, 5.0, 60.0),
            "edge_margin_mm": _num(form, "edge_margin_mm", d.edge_margin_mm, 0.0, 20.0),
            "ligament_margin_mm": _num(form, "ligament_margin_mm", d.ligament_margin_mm, 0.0, 40.0),
            "save_segmentation": _on(form.get("save_segmentation")),
            "keep_edit_data": _on(form.get("keep_edit_data"), True)}


def _safe_rel(name: str) -> Path:
    parts = [p for p in re.split(r"[\\/]+", name or "") if p not in ("", ".", "..")]
    parts = [safe_name(p.replace(":", "_"), "f") for p in parts]
    return Path(*parts) if parts else Path("file")


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class LoginGuard:
    """Stops password guessing: after `max_wrong` DIFFERENT wrong passwords from one sender within
    `window` seconds, that sender is refused for `lock` seconds. Repeating the same wrong password
    (e.g. a browser tab still holding an old password) does not count again."""

    def __init__(self, max_wrong: int = 10, window: float = 15 * 60, lock: float = 15 * 60):
        self.max_wrong, self.window, self.lock = max_wrong, window, lock
        self._key = secrets.token_bytes(16)          # wrong passwords are kept only as keyed hashes
        self._wrong: dict[str, dict[bytes, float]] = {}
        self._locked: dict[str, float] = {}
        self._mx = threading.Lock()

    @staticmethod
    def sender(request: Request) -> str:
        host = request.client.host if request.client else "?"
        if host in LOOPBACK:
            # Behind Tailscale Serve every request arrives from this computer; tell people apart by
            # the identity Tailscale adds (it strips any copy sent by the browser) or the forwarded address.
            fwd = (request.headers.get("tailscale-user-login")
                   or request.headers.get("x-forwarded-for", "").split(",")[0].strip())
            if fwd:
                return "via " + fwd[:200]
        return host

    def wait_seconds(self, who: str) -> float:
        with self._mx:
            until = self._locked.get(who, 0.0)
            left = until - time.monotonic()
            if left <= 0 and who in self._locked:
                del self._locked[who]
            return max(0.0, left)

    def wrong(self, who: str, password: str) -> None:
        now = time.monotonic()
        tag = hmac.new(self._key, password.encode("utf-8", "replace"), hashlib.sha256).digest()
        with self._mx:
            if len(self._wrong) > 5000:              # bound memory
                self._wrong.clear()
            seen = {t: ts for t, ts in self._wrong.get(who, {}).items() if now - ts < self.window}
            seen[tag] = now
            self._wrong[who] = seen
            if len(seen) >= self.max_wrong:
                self._locked[who] = now + self.lock
                self._wrong.pop(who, None)

    def right(self, who: str) -> None:
        with self._mx:
            self._wrong.pop(who, None)


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "content-security-policy": "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


class SecurityMiddleware:
    """Refuses changes sent from other websites (cross-site request forgery), adds browser security
    headers, and tells browsers not to keep copies of pages and results (patient data) in their cache."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["method"] not in SAFE_METHODS:
            site = next((v.decode("latin-1").lower() for k, v in scope.get("headers", [])
                         if k.lower() == b"sec-fetch-site"), "")
            if site in ("cross-site", "same-site"):
                resp = JSONResponse({"detail": "Refused: this request came from another website."}, status_code=403)
                return await resp(scope, receive, send)
        static = scope.get("path", "").startswith("/static/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                for k, v in SECURITY_HEADERS.items():
                    if k not in h:
                        h[k] = v
                if not static and "cache-control" not in h:
                    h["cache-control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_headers)


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
           "revision": res.get("revision", 0), "editable": bool(res.get("editable")),
           "quality": res.get("quality"), "quality_notes": res.get("quality_notes") or [],
           "scan_complete": res.get("scan_complete"), "completeness_notes": res.get("completeness_notes") or [],
           "contrast_phase": res.get("contrast_phase"), "contrast_reason": res.get("contrast_phase_reason"),
           "series": (res.get("input") or {}).get("series_description"),
           "slice_mm": (res.get("input") or {}).get("slice_thickness_mm")}
    for k in ("LL", "RL1", "RL2", "SPL"):
        r = rois.get(k) or {}
        out[k] = {f: r.get(f) for f in ("placed", "mean_hu", "sd_hu", "min_hu", "max_hu", "area_cm2",
                                        "diameter_mm", "z_mm", "dicom_instance", "segment_at_center",
                                        "side_of_ligament", "side_ok", "manual", "edited_by", "hu_check")}
        out[k]["hu_range"] = (res.get("hu_ranges") or {}).get(k)
    return out


def create_app(data: Path, keep_uploads: bool = False, start_worker: bool = True) -> FastAPI:
    data = Path(data)
    store = Store(data)
    worker = Worker(store, keep_uploads=keep_uploads)
    intake = Intake(store, keep_uploads=keep_uploads, on_ready=worker.wake.set)

    @asynccontextmanager
    async def lifespan(_app):
        if start_worker:
            for t in (intake, worker):
                if not t.is_alive():
                    t.start()
        yield

    app = FastAPI(title="HUnter", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store, app.state.worker, app.state.intake = store, worker, intake
    app.add_middleware(SecurityMiddleware)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    security = HTTPBasic(realm="HUnter lab website")
    guard = app.state.login_guard = LoginGuard()

    def user(request: Request, creds: HTTPBasicCredentials = Depends(security)) -> str:
        who = guard.sender(request)
        wait = guard.wait_seconds(who)
        if wait:
            mins = max(1, math.ceil(wait / 60))
            raise HTTPException(429, f"Too many wrong passwords. Try again in {mins} minute{'s' if mins > 1 else ''}.",
                                headers={"Retry-After": str(int(wait) + 1)})
        if not auth.check_password(data, creds.password):
            guard.wrong(who, creds.password)
            raise HTTPException(401, "Wrong password", headers={"WWW-Authenticate": 'Basic realm="HUnter"'})
        guard.right(who)
        return (creds.username or "lab").strip()[:60] or "lab"

    def page(request: Request, name: str, **ctx):
        return templates.TemplateResponse(request, name, {"version": __version__, "defaults": Settings(), **ctx})

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, u: str = Depends(user)):
        return page(request, "index.html", user=u)

    @app.get("/jobs/{jid}", response_class=HTMLResponse)
    def job_page(request: Request, jid: int, case: int | None = None, u: str = Depends(user)):
        job = store.q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
        if not job:
            raise HTTPException(404, "No such upload")
        return page(request, "job.html", user=u, job=job, only=case)

    @app.get("/library", response_class=HTMLResponse)
    def library_page(request: Request, u: str = Depends(user)):
        return page(request, "library.html", user=u, active="library")

    @app.get("/about", response_class=HTMLResponse)
    def about(request: Request, u: str = Depends(user)):
        return page(request, "about.html", user=u)

    # ---------- API ----------
    @app.post("/api/jobs")
    async def create_job(request: Request, u: str = Depends(user)):
        form = await request.form()
        s = Settings(**_measure_settings(form),
                     preferred_slice_mm=_num(form, "preferred_slice_mm", Settings().preferred_slice_mm, 0.5, 10.0))
        label = safe_name(str(form.get("label") or ""), "")[:40]
        jid = store.create_job(label, u, s, _on(form.get("include_identifiers")), library=_on(form.get("library")))
        return {"job_id": jid}

    def _check_disk():
        if shutil.disk_usage(data).free < MIN_FREE_BYTES:
            raise HTTPException(507, "This computer is almost out of disk space. Delete studies from the "
                                     "library (or other files) and try again.")

    @app.post("/api/jobs/{jid}/files")
    async def add_files(request: Request, jid: int, u: str = Depends(user)):
        job = store.q("SELECT status FROM jobs WHERE id=?", (jid,), one=True)
        if not job or job["status"] != "uploading":
            raise HTTPException(409, "This upload is closed")
        _check_disk()
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
            intake.wake.set()
        return {"ok": True}

    @app.post("/api/jobs/{jid}/delete")
    def delete_job(jid: int, u: str = Depends(user)):
        job = store.q("SELECT status FROM jobs WHERE id=?", (jid,), one=True)
        if not job:
            raise HTTPException(404)
        store.x("UPDATE jobs SET status='cancelled' WHERE id=?", (jid,))
        for r in store.q("SELECT id FROM cases WHERE job_id=?", (jid,)):
            worker.stop_case(r["id"])
        store.delete_job(jid)
        return {"ok": True}

    @app.get("/api/jobs")
    def list_jobs(u: str = Depends(user)):
        jobs = store.q("SELECT id, label, submitted_by, created_at, status, n_cases, n_done, n_files, message "
                       "FROM jobs WHERE COALESCE(library, 0)=0 ORDER BY id DESC LIMIT 200")
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
        places = store.queue_positions()
        ids = {r["id"] for r in rows}
        mine = [p for cid, p in places.items() if cid in ids]
        # studies from other uploads (or the library) that are measured before this upload's next one
        ahead = sum(1 for cid, p in places.items() if cid not in ids and p < min(mine)) if mine else 0
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

    # ---------- manual circle editor ----------
    from .. import edit as editmod
    from collections import OrderedDict
    import threading
    _ed_cache: "OrderedDict[int, editmod.EditData]" = OrderedDict()
    _ed_lock = threading.Lock()

    def _edit_data(case_id: int):
        row = _case_row(case_id)
        d = store.case_dir(row["job_id"], row["id"])
        if not editmod.EditData.available(d):
            raise HTTPException(409, "This scan has no saved data for editing (measured before editing "
                                     "was added, or with editing turned off). Upload it again to adjust circles.")
        with _ed_lock:
            if case_id not in _ed_cache:
                _ed_cache[case_id] = editmod.EditData(d)
                while len(_ed_cache) > 2:
                    _ed_cache.popitem(last=False)
            _ed_cache.move_to_end(case_id)
            return row, d, _ed_cache[case_id]

    @app.get("/cases/{case_id}/edit", response_class=HTMLResponse)
    def edit_page(request: Request, case_id: int, u: str = Depends(user)):
        row = _case_row(case_id)
        return page(request, "edit.html", user=u, case=row)

    @app.get("/api/cases/{case_id}/edit-info")
    def edit_info(case_id: int, u: str = Depends(user)):
        row = _case_row(case_id)
        d = store.case_dir(row["job_id"], row["id"])
        res = json.loads(row["result_json"] or "{}")
        if not editmod.EditData.available(d):
            return {"available": False, "case_id": row["case_id"], "job_id": row["job_id"]}
        _, _, ed = _edit_data(case_id)
        X, Y, Z = ed.ct.shape
        organ_c = {k: np.argwhere(m) for k, m in (("liver", ed.seg > 0), ("spleen", ed.spleen > 0))}
        out = []
        by_key = {r["key"]: r for r in res.get("rois", [])}
        for key, spec in editmod.SPECS.items():
            r = by_key.get(key, {"key": key, "name": spec.name, "placed": False})
            if r.get("placed") and r.get("center_voxel"):
                x, y, z = ed.to_crop(r["center_voxel"])
            else:   # not placed automatically: start in the middle of the organ
                pts = organ_c["spleen" if key == "SPL" else "liver"]
                x, y, z = (int(v) for v in (np.median(pts, axis=0) if len(pts) else (X // 2, Y // 2, Z // 2)))
            out.append({"key": key, "name": spec.name, "placed": bool(r.get("placed")), "x": x, "y": y, "z": z,
                        "diameter_mm": r.get("diameter_mm") or Settings().roi_diameter_mm,
                        "manual": bool(r.get("manual")), "edited_by": r.get("edited_by"),
                        "auto": {k: (r.get("auto") or {}).get(k) for k in editmod.STAT_FIELDS} if r.get("manual") else None,
                        "reason": r.get("reason")})
        return {"available": True, "case_id": row["case_id"], "job_id": row["job_id"],
                "shape": [X, Y, Z], "spacing": list(ed.spacing), "rois": out,
                "hu_ranges": res.get("hu_ranges") or {}, "contrast_phase": res.get("contrast_phase")}

    @app.get("/cases/{case_id}/slice/{z}.png")
    def edit_slice(case_id: int, z: int, u: str = Depends(user)):
        _, _, ed = _edit_data(case_id)
        return StreamingResponse(io.BytesIO(editmod.slice_png(ed, z)), media_type="image/png",
                                 headers={"Cache-Control": "private, max-age=600"})

    def _measure_payload(ed, key, x, y, z, d):
        if key not in editmod.SPECS:
            raise HTTPException(404, "unknown circle")
        r = editmod.measure_at(ed, key, x, y, z, d)
        cx, cy, cz = ed.to_crop(r.center_voxel)
        chk = editmod.check_circle(ed, key, cx, cy, cz, r.diameter_mm)
        return r, {"x": cx, "y": cy, "z": cz, **{k: getattr(r, k) for k in editmod.STAT_FIELDS},
                   "n_pixels": r.n_pixels, "z_mm": r.z_mm, "dicom_instance": r.dicom_instance,
                   "segment_at_center": r.segment_at_center, "side_of_ligament": r.side_of_ligament, **chk}

    @app.get("/api/cases/{case_id}/measure")
    def edit_measure(case_id: int, key: str, x: int, y: int, z: int, d: float, u: str = Depends(user)):
        _, _, ed = _edit_data(case_id)
        return _measure_payload(ed, key, x, y, z, d)[1]

    def _save_result(row, case_dir, res):
        lm, sm = res.get("liver_mean_hu_3roi"), res.get("spleen_mean_hu")
        title = row["case_id"] + (f"   liver mean (3 ROIs) {lm:.1f} HU" if lm is not None else "") + \
            (f"   spleen {sm:.1f} HU" if sm is not None else "")
        editmod.redraw_qa(case_dir, res, title)
        text = json.dumps(res, indent=2, default=str)
        (case_dir / "result.json").write_text(text)
        store.x("UPDATE cases SET result_json=?, status=? WHERE id=?", (text, res.get("status", row["status"]), row["id"]))

    @app.post("/api/cases/{case_id}/rois/{key}")
    async def edit_save(request: Request, case_id: int, key: str, u: str = Depends(user)):
        body = await request.json()
        row, case_dir, ed = _edit_data(case_id)
        try:
            x, y, z, d = int(body["x"]), int(body["y"]), int(body["z"]), float(body["d"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "x, y, z and d are required")
        r, payload = _measure_payload(ed, key, x, y, z, d)
        res = editmod.apply_manual(json.loads(row["result_json"] or "{}"), r, u)
        await run_in_threadpool(_save_result, row, case_dir, res)
        return {"ok": True, "measure": payload, "liver_mean": res.get("liver_mean_hu_3roi"),
                "spleen_mean": res.get("spleen_mean_hu")}

    @app.post("/api/cases/{case_id}/rois/{key}/reset")
    async def edit_reset(case_id: int, key: str, u: str = Depends(user)):
        row, case_dir, _ = _edit_data(case_id)
        res = editmod.reset_to_auto(json.loads(row["result_json"] or "{}"), key)
        await run_in_threadpool(_save_result, row, case_dir, res)
        return {"ok": True}

    # ---------- scan library ----------
    @app.get("/api/library")
    def library_list(u: str = Depends(user)):
        rows = store.q("SELECT c.id, c.job_id, c.case_id, c.status, c.result_json, c.info_json, c.current_step,"
                       " c.started_at, c.finished_at, j.label, j.created_at, j.submitted_by "
                       "FROM cases c JOIN jobs j ON j.id = c.job_id WHERE j.library=1 ORDER BY c.id DESC")
        places = store.queue_positions()
        keys: dict[str, list[int]] = {}
        studies = []
        for r in rows:
            info = json.loads(r["info_json"] or "{}")
            res = json.loads(r["result_json"]) if r.get("result_json") else {}
            rois = {x["key"]: x for x in res.get("rois", [])}
            if info.get("study_key"):
                keys.setdefault(info["study_key"], []).append(r["id"])
            studies.append({
                "id": r["id"], "job_id": r["job_id"], "name": r["case_id"], "status": r["status"],
                "batch": r["label"] or "", "added_at": r["created_at"], "added_by": r["submitted_by"],
                "step": r["current_step"] or "", "started_at": r["started_at"], "place": places.get(r["id"]),
                "info": {k: v for k, v in info.items() if k != "study_key"}, "study_key": info.get("study_key"),
                "measured": bool(res) and res.get("status") in ("ok", "partial"),
                "measured_at": r["finished_at"] if res else None,
                "liver_mean": res.get("liver_mean_hu_3roi"), "spleen_mean": res.get("spleen_mean_hu"),
                "quality": res.get("quality"), "scan_complete": res.get("scan_complete"),
                "hu_warning": any((x or {}).get("hu_check") for x in rois.values()),
                "manual": any((x or {}).get("manual") for x in rois.values()),
                "error": res.get("error")})
        for s in studies:   # the same study uploaded more than once
            others = [i for i in keys.get(s.pop("study_key") or "", []) if i != s["id"]]
            s["same_study_as"] = others
        pending = store.q("SELECT id, label, status, n_files, message FROM jobs WHERE library=1 "
                          "AND status IN ('uploading','queued','failed') ORDER BY id DESC")
        avg = store.q("SELECT AVG(seconds) AS a FROM cases WHERE status IN ('ok','partial')", one=True)["a"]
        du = shutil.disk_usage(data)
        return {"studies": studies, "batches": pending, "avg_seconds_per_case": avg,
                "waiting": len(places), "stored_bytes": sum(s["info"].get("size_bytes") or 0 for s in studies),
                "free_bytes": du.free}

    async def _ids(request: Request) -> tuple[list[int], dict]:
        try:
            body = await request.json()
            ids = [int(i) for i in body.get("ids", [])][:5000]
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(400, "send {\"ids\": [...]}")
        return list(dict.fromkeys(ids)), body

    @app.post("/api/library/measure")
    async def library_measure(request: Request, u: str = Depends(user)):
        ids, body = await _ids(request)
        queued = store.queue_cases(ids, _measure_settings(body))
        with _ed_lock:
            for cid in queued:
                _ed_cache.pop(cid, None)
        worker.wake.set()
        return {"queued": queued}

    @app.post("/api/library/unqueue")
    async def library_unqueue(request: Request, u: str = Depends(user)):
        ids, _ = await _ids(request)
        return {"unqueued": store.unqueue_cases(ids)}

    @app.post("/api/library/delete")
    async def library_delete(request: Request, u: str = Depends(user)):
        ids, _ = await _ids(request)
        lib = {r["id"] for r in store.q("SELECT c.id FROM cases c JOIN jobs j ON j.id = c.job_id WHERE j.library=1")}
        deleted = []
        for cid in ids:
            if cid not in lib:
                continue
            worker.stop_case(cid)
            with _ed_lock:
                _ed_cache.pop(cid, None)
            await run_in_threadpool(store.delete_case, cid)
            deleted.append(cid)
        return {"deleted": deleted}

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

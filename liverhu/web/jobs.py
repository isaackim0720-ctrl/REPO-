"""Job store (SQLite), the intake step and the single measuring worker.

One upload = one job; a job holds one case per study found in it. Two kinds of upload:

* "measure now": every study is put in the queue straight away, and the scan files are
  deleted once all of them are measured;
* library: studies are only stored (status 'stored'). People pick studies later and add them
  to the queue; the files stay until the study is deleted.

All queued studies, from any upload, form ONE queue (in the order they were queued) and are
measured ONE AT A TIME, each in a fresh subprocess, so an 8 GB lab PC never runs two models at
once. Reading new uploads (finding the studies) runs separately, so it never waits for a
measurement to finish.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..config import Settings
from ..scanio import CaseInput, discover_cases

CASE_TIMEOUT_S = 60 * 60
FINISHED = ("ok", "partial", "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT, submitted_by TEXT, created_at TEXT, status TEXT,
  n_cases INTEGER DEFAULT 0, n_done INTEGER DEFAULT 0, n_files INTEGER DEFAULT 0,
  message TEXT DEFAULT '', settings_json TEXT, include_identifiers INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER, case_id TEXT, status TEXT, result_json TEXT,
  started_at TEXT, finished_at TEXT, seconds REAL, current_step TEXT DEFAULT ''
);
"""
# columns added after v0.7 (added to older databases on start)
MIGRATIONS = [("jobs", "library", "INTEGER DEFAULT 0"),
              ("cases", "queue_order", "REAL"),
              ("cases", "info_json", "TEXT")]


def relocate_case_file(case_dir: Path) -> None:
    """If the HUnter folder was moved (e.g. out of Downloads) while scans were waiting, point the
    waiting scan's saved file paths at the new location."""
    f = Path(case_dir) / "case.json"
    try:
        job = json.loads(f.read_text())
        old_case_dir = Path(job["out_dir"])
    except (OSError, ValueError, KeyError):
        return
    if old_case_dir == Path(case_dir):
        return
    old_root, new_root = str(old_case_dir.parents[3]), str(Path(case_dir).parents[3])   # .../<data>/jobs/<j>/cases/<c>

    def move(p):
        return new_root + p[len(old_root):] if isinstance(p, str) and p.startswith(old_root) else p
    case = job.get("case") or {}
    if "path" in case:
        case["path"] = move(case["path"])
    if isinstance(case.get("files"), list):
        case["files"] = [move(p) for p in case["files"]]
    job["out_dir"] = str(case_dir)
    f.write_text(json.dumps(job))


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def case_info(case: CaseInput) -> dict:
    """What the library shows about a stored study (before it is measured)."""
    meta = case.meta or {}
    size = 0
    for f in case.study_files or ([case.path] if case.path else case.files):
        try:
            size += os.path.getsize(f)
        except OSError:
            pass
    info = {"kind": case.kind, "series": meta.get("series_description") or "",
            "n_images": meta.get("n_images"), "slice_mm": meta.get("slice_thickness_mm"),
            "study_key": meta.get("study_key"), "contrast_tag": meta.get("contrast_agent_tag") or "",
            "size_bytes": size}
    for k in ("patient_id", "accession_number", "study_date"):
        if meta.get(k):
            info[k] = meta[k]
    if case.kind == "nifti" and case.path:
        info["series"] = Path(case.path).name
        try:
            import nibabel as nib
            h = nib.load(case.path).header            # reads only the header
            info["n_images"] = int(h.get_data_shape()[2])
            info["slice_mm"] = round(float(h.get_zooms()[2]), 2)
        except Exception:
            pass
    return info


class Store:
    def __init__(self, data: Path):
        self.data = Path(data)
        self.db_path = self.data / "liverhu.sqlite"
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            for table, col, decl in MIGRATIONS:
                if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            # a study that was being measured when the computer stopped is measured again
            c.execute("UPDATE cases SET status='queued', current_step='' WHERE status='running'")

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def q(self, sql: str, args=(), one=False):
        with self._lock, self._conn() as c:
            cur = c.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()]
            return (rows[0] if rows else None) if one else rows

    def x(self, sql: str, args=()) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(sql, args)
            return cur.lastrowid

    def job_dir(self, job_id: int) -> Path:
        return self.data / "jobs" / str(job_id)

    def upload_dir(self, job_id: int) -> Path:
        return self.job_dir(job_id) / "upload"

    def case_dir(self, job_id: int, case_row_id: int) -> Path:
        return self.job_dir(job_id) / "cases" / str(case_row_id)

    def create_job(self, label: str, user: str, settings: Settings, include_identifiers: bool,
                   library: bool = False) -> int:
        jid = self.x("INSERT INTO jobs (label, submitted_by, created_at, status, settings_json, include_identifiers,"
                     " library) VALUES (?,?,?,?,?,?,?)",
                     (label, user, now(), "uploading", json.dumps(settings.to_dict()), int(include_identifiers),
                      int(library)))
        self.upload_dir(jid).mkdir(parents=True, exist_ok=True)
        return jid

    def delete_job(self, job_id: int) -> None:
        self.x("DELETE FROM cases WHERE job_id=?", (job_id,))
        self.x("DELETE FROM jobs WHERE id=?", (job_id,))
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)

    # ---------- queue ----------
    def next_queue_order(self) -> float:
        return float(self.q("SELECT COALESCE(MAX(queue_order), 0) AS m FROM cases", one=True)["m"]) + 1

    def add_cases(self, jid: int, cases: list[CaseInput], settings: Settings, queue: bool) -> None:
        """Insert the studies found in an upload, either queued (measure now) or only stored."""
        up = self.upload_dir(jid).resolve()
        order = self.next_queue_order()
        for i, case in enumerate(cases):
            info = case_info(case)
            cid = self.x("INSERT INTO cases (job_id, case_id, status, queue_order, info_json) VALUES (?,?,?,?,?)",
                         (jid, case.case_id, "queued" if queue else "stored", order + i if queue else None,
                          json.dumps(info)))
            d = self.case_dir(jid, cid)
            d.mkdir(parents=True, exist_ok=True)
            rel = []
            for f in case.study_files or ([case.path] if case.path else case.files):
                p = Path(f).resolve()
                if up in p.parents:
                    rel.append(str(p.relative_to(up)))
            (d / "files.json").write_text(json.dumps(rel))          # for deleting the study later
            (d / "case.json").write_text(json.dumps({"case": case.to_dict(), "settings": settings.to_dict(),
                                                     "out_dir": str(d)}))

    def queue_cases(self, ids: list[int], overrides: dict) -> list[int]:
        """Put stored library studies in the measuring queue, in the given order. Returns the ids queued."""
        order = self.next_queue_order()
        queued = []
        for cid in ids:
            row = self.q("SELECT c.*, j.library FROM cases c JOIN jobs j ON j.id = c.job_id WHERE c.id=?",
                         (cid,), one=True)
            if not row or not row["library"] or row["status"] in ("queued", "running"):
                continue
            f = self.case_dir(row["job_id"], cid) / "case.json"
            try:
                job = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            job["settings"] = Settings.from_dict({**(job.get("settings") or {}), **overrides}).to_dict()
            f.write_text(json.dumps(job))
            self.x("UPDATE cases SET status='queued', queue_order=?, current_step='' WHERE id=?",
                   (order + len(queued), cid))
            queued.append(cid)
        return queued

    def unqueue_cases(self, ids: list[int]) -> list[int]:
        """Take waiting library studies out of the queue again (back to stored, or to their last result)."""
        done = []
        for cid in ids:
            row = self.q("SELECT c.*, j.library FROM cases c JOIN jobs j ON j.id = c.job_id WHERE c.id=?",
                         (cid,), one=True)
            if not row or not row["library"] or row["status"] != "queued":
                continue
            old = json.loads(row["result_json"]).get("status") if row.get("result_json") else None
            self.x("UPDATE cases SET status=?, queue_order=NULL WHERE id=? AND status='queued'",
                   (old if old in FINISHED else "stored", cid))
            done.append(cid)
        return done

    def delete_case(self, cid: int) -> None:
        """Delete one study: its scan files, results and row. An upload left empty is removed too."""
        row = self.q("SELECT * FROM cases WHERE id=?", (cid,), one=True)
        if not row:
            return
        jid = row["job_id"]
        d = self.case_dir(jid, cid)
        up = self.upload_dir(jid).resolve()
        try:
            rel = json.loads((d / "files.json").read_text())
        except (OSError, ValueError):
            rel = []
        dirs = set()
        for r in rel:
            p = (up / r).resolve()
            if up in p.parents:
                try:
                    p.unlink()
                except OSError:
                    pass
                dirs.update(q for q in p.parents if up in q.parents)
        for q in sorted(dirs, key=lambda q: len(q.parts), reverse=True):   # remove folders left empty
            try:
                q.rmdir()
            except OSError:
                pass
        shutil.rmtree(d, ignore_errors=True)
        self.x("DELETE FROM cases WHERE id=?", (cid,))
        left = self.q("SELECT COUNT(*) AS n FROM cases WHERE job_id=?", (jid,), one=True)["n"]
        if left == 0:
            self.delete_job(jid)
        else:
            done = self.q(f"SELECT COUNT(*) AS n FROM cases WHERE job_id=? AND status IN {FINISHED}",
                          (jid,), one=True)["n"]
            self.x("UPDATE jobs SET n_cases=?, n_done=? WHERE id=?", (left, done, jid))

    def queue_positions(self) -> dict[int, int]:
        """case id -> place in line (0 = being measured now, 1 = next, ...)."""
        rows = self.q("SELECT id, status FROM cases WHERE status IN ('queued','running') "
                      "ORDER BY status='running' DESC, COALESCE(queue_order, 0), id")
        running = sum(1 for r in rows if r["status"] == "running")
        return {r["id"]: (0 if r["status"] == "running" else i - running + 1) for i, r in enumerate(rows)}


class Intake(threading.Thread):
    """Reads each finished upload and finds its studies (runs beside the measuring worker)."""

    def __init__(self, store: Store, keep_uploads: bool = False, on_ready=None):
        super().__init__(daemon=True, name="liverhu-intake")
        self.store = store
        self.keep_uploads = keep_uploads
        self.on_ready = on_ready or (lambda: None)
        self.wake = threading.Event()

    def run(self):
        while True:
            try:
                busy = self.intake_once()
            except Exception:          # never let the thread die
                busy = False
            if not busy:
                self.wake.wait(timeout=3)
                self.wake.clear()

    def intake_once(self) -> bool:
        s = self.store
        job = s.q("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1", one=True)
        if not job:
            return False
        jid, library = job["id"], bool(job.get("library"))
        if s.q("SELECT COUNT(*) AS n FROM cases WHERE job_id=?", (jid,), one=True)["n"]:
            # read before a restart (or by an older version): just carry on
            s.x("UPDATE jobs SET status=? WHERE id=?", ("stored" if library else "running", jid))
            self.on_ready()
            return True
        s.x("UPDATE jobs SET message='reading the files' WHERE id=?", (jid,))
        settings = Settings.from_dict(json.loads(job["settings_json"] or "{}"))
        try:
            cases, warns = discover_cases(s.upload_dir(jid), label=job["label"] or "",
                                          preferred_slice_mm=settings.preferred_slice_mm,
                                          include_identifiers=bool(job["include_identifiers"]))
            s.add_cases(jid, cases, settings, queue=not library)
        except Exception as e:
            cases, warns = [], [f"could not read the upload: {type(e).__name__}: {e}"]
        msg = " | ".join(w for w in warns if cases or not w.startswith("No CT scans found"))
        if not cases:
            s.x("UPDATE jobs SET status='failed', message=? WHERE id=?", (msg or "no CT scans found in the upload", jid))
            if library or not self.keep_uploads:
                shutil.rmtree(s.upload_dir(jid), ignore_errors=True)
            return True
        s.x("UPDATE jobs SET status=?, n_cases=?, message=? WHERE id=?",
            ("stored" if library else "running", len(cases), msg, jid))
        self.on_ready()
        return True


class Worker(threading.Thread):
    """Measures the queued studies one at a time, in queue order."""

    def __init__(self, store: Store, keep_uploads: bool = False):
        super().__init__(daemon=True, name="liverhu-worker")
        self.store = store
        self.keep_uploads = keep_uploads
        self.wake = threading.Event()
        self.current_proc: subprocess.Popen | None = None
        self.current_case: int | None = None

    def run(self):
        while True:
            try:
                busy = self.run_next()
            except Exception:          # never let the worker thread die
                busy = False
            if not busy:
                self.wake.wait(timeout=3)
                self.wake.clear()

    def next_case(self) -> dict | None:
        return self.store.q("SELECT c.* FROM cases c JOIN jobs j ON j.id = c.job_id "
                            "WHERE c.status='queued' AND j.status IN ('running','stored') "
                            "ORDER BY COALESCE(c.queue_order, 0), c.id LIMIT 1", one=True)

    def run_next(self) -> bool:
        row = self.next_case()
        if not row:
            return False
        self._run_case(row["job_id"], row)
        self._after_case(row["job_id"])
        return True

    def _after_case(self, jid: int) -> None:
        s = self.store
        job = s.q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
        if not job:
            return            # deleted meanwhile
        done = s.q(f"SELECT COUNT(*) AS n FROM cases WHERE job_id=? AND status IN {FINISHED}", (jid,), one=True)["n"]
        s.x("UPDATE jobs SET n_done=? WHERE id=?", (done, jid))
        if job.get("library") or job["status"] != "running":
            return
        left = s.q("SELECT COUNT(*) AS n FROM cases WHERE job_id=? AND status IN ('queued','running')",
                   (jid,), one=True)["n"]
        if not left:
            s.x("UPDATE jobs SET status='done' WHERE id=?", (jid,))
            if not self.keep_uploads:
                shutil.rmtree(s.upload_dir(jid), ignore_errors=True)

    def stop_case(self, cid: int) -> None:
        """Stop measuring this study now (it is being deleted)."""
        if self.current_case == cid and self.current_proc:
            self.current_proc.kill()

    def _run_case(self, jid: int, row: dict):
        s = self.store
        d = s.case_dir(jid, row["id"])
        relocate_case_file(d)
        for old in ("result.json", "qa.png"):     # measured before (library): start clean
            (d / old).unlink(missing_ok=True)
        s.x("UPDATE cases SET status='running', started_at=?, current_step='starting' WHERE id=?", (now(), row["id"]))
        t0 = time.time()
        pkg_root = str(Path(__file__).resolve().parents[2])
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
                   PYTHONPATH=pkg_root + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""))
        log_path = d / "log.txt"
        self.current_case = row["id"]
        try:
            with open(log_path, "w", encoding="utf-8") as log:
                # below-normal priority keeps the computer responsive while a scan is measured
                extra = {"creationflags": subprocess.BELOW_NORMAL_PRIORITY_CLASS} if os.name == "nt" else {}
                proc = subprocess.Popen([sys.executable, "-m", "liverhu.worker", str(d / "case.json")],
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                                        encoding="utf-8", errors="replace", bufsize=1, **extra)
                self.current_proc = proc
                for line in proc.stdout:
                    log.write(line)
                    log.flush()
                    txt = line.strip()
                    if txt.startswith("[") and "]" in txt:
                        s.x("UPDATE cases SET current_step=? WHERE id=?", (txt.split("]", 1)[1].strip()[:120], row["id"]))
                    if time.time() - t0 > CASE_TIMEOUT_S:
                        proc.kill()
                        break
                proc.wait(timeout=60)
        except OSError:
            proc = None           # the study was deleted while it was being measured
        finally:
            self.current_proc = None
            self.current_case = None
        res_path = d / "result.json"
        if res_path.exists():
            res = json.loads(res_path.read_text())
            status = res.get("status", "failed")
        else:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:] if log_path.exists() else ""
            code = proc.returncode if proc else None
            hint = " (the computer may have run out of memory)" if code not in (0, None) else ""
            res = {"case_id": row["case_id"], "status": "failed",
                   "error": f"processing stopped unexpectedly{hint}", "log_tail": tail, "flags": []}
            status = "failed"
        s.x("UPDATE cases SET status=?, result_json=?, finished_at=?, seconds=?, current_step='' WHERE id=?",
            (status, json.dumps(res), now(), round(time.time() - t0, 1), row["id"]))

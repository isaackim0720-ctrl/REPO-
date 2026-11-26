"""Job store (SQLite) and the single background worker.

One upload = one job; a job can contain many cases (studies). Cases are processed ONE AT
A TIME, each in a fresh subprocess, so an 8 GB lab PC never runs two models at once.
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
from ..scanio import discover_cases

CASE_TIMEOUT_S = 60 * 60

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


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, data: Path):
        self.data = Path(data)
        self.db_path = self.data / "liverhu.sqlite"
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            # resume after a restart
            c.execute("UPDATE jobs SET status='queued' WHERE status='running'")
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

    def create_job(self, label: str, user: str, settings: Settings, include_identifiers: bool) -> int:
        jid = self.x("INSERT INTO jobs (label, submitted_by, created_at, status, settings_json, include_identifiers)"
                     " VALUES (?,?,?,?,?,?)",
                     (label, user, now(), "uploading", json.dumps(settings.to_dict()), int(include_identifiers)))
        self.upload_dir(jid).mkdir(parents=True, exist_ok=True)
        return jid

    def delete_job(self, job_id: int) -> None:
        self.x("DELETE FROM cases WHERE job_id=?", (job_id,))
        self.x("DELETE FROM jobs WHERE id=?", (job_id,))
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)


class Worker(threading.Thread):
    def __init__(self, store: Store, keep_uploads: bool = False):
        super().__init__(daemon=True, name="liverhu-worker")
        self.store = store
        self.keep_uploads = keep_uploads
        self.wake = threading.Event()
        self.current_proc: subprocess.Popen | None = None

    def run(self):
        while True:
            job = self.store.q("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1", one=True)
            if not job:
                self.wake.wait(timeout=3)
                self.wake.clear()
                continue
            try:
                self._run_job(job)
            except Exception as e:   # never let the worker thread die
                self.store.x("UPDATE jobs SET status='failed', message=? WHERE id=?", (f"{type(e).__name__}: {e}", job["id"]))

    def _run_job(self, job: dict):
        s = self.store
        jid = job["id"]
        s.x("UPDATE jobs SET status='running', message='finding scans' WHERE id=?", (jid,))
        settings = Settings.from_dict(json.loads(job["settings_json"] or "{}"))
        existing = s.q("SELECT id FROM cases WHERE job_id=?", (jid,))
        if not existing:
            up = s.upload_dir(jid)
            cases, warns = discover_cases(up, label=job["label"] or "", preferred_slice_mm=settings.preferred_slice_mm,
                                          include_identifiers=bool(job["include_identifiers"]))
            for case in cases:
                cid = s.x("INSERT INTO cases (job_id, case_id, status) VALUES (?,?,?)", (jid, case.case_id, "queued"))
                d = s.case_dir(jid, cid)
                d.mkdir(parents=True, exist_ok=True)
                (d / "case.json").write_text(json.dumps({"case": case.to_dict(), "settings": settings.to_dict(),
                                                         "out_dir": str(d)}))
            s.x("UPDATE jobs SET n_cases=?, message=? WHERE id=?", (len(cases), " | ".join(warns), jid))
        rows = s.q("SELECT * FROM cases WHERE job_id=? AND status='queued' ORDER BY id", (jid,))
        for row in rows:
            if (s.q("SELECT status FROM jobs WHERE id=?", (jid,), one=True) or {}).get("status") != "running":
                return   # job deleted/cancelled meanwhile
            self._run_case(jid, row)
            done = s.q("SELECT COUNT(*) AS n FROM cases WHERE job_id=? AND status NOT IN ('queued','running')",
                       (jid,), one=True)["n"]
            s.x("UPDATE jobs SET n_done=? WHERE id=?", (done, jid))
        total = s.q("SELECT COUNT(*) AS n FROM cases WHERE job_id=?", (jid,), one=True)["n"]
        status = "done" if total else "failed"
        msg = s.q("SELECT message FROM jobs WHERE id=?", (jid,), one=True)["message"] or ""
        if not total and not msg:
            msg = "no CT scans found in the upload"
        s.x("UPDATE jobs SET status=?, message=? WHERE id=?", (status, msg, jid))
        if not self.keep_uploads:
            shutil.rmtree(s.upload_dir(jid), ignore_errors=True)

    def _run_case(self, jid: int, row: dict):
        s = self.store
        d = s.case_dir(jid, row["id"])
        s.x("UPDATE cases SET status='running', started_at=?, current_step='starting' WHERE id=?", (now(), row["id"]))
        t0 = time.time()
        pkg_root = str(Path(__file__).resolve().parents[2])
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
                   PYTHONPATH=pkg_root + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""))
        log_path = d / "log.txt"
        with open(log_path, "w", encoding="utf-8") as log:
            proc = subprocess.Popen([sys.executable, "-m", "liverhu.worker", str(d / "case.json")],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                                    encoding="utf-8", errors="replace", bufsize=1)
            self.current_proc = proc
            try:
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
            finally:
                self.current_proc = None
        res_path = d / "result.json"
        if res_path.exists():
            res = json.loads(res_path.read_text())
            status = res.get("status", "failed")
        else:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:] if log_path.exists() else ""
            hint = " (the computer may have run out of memory)" if proc.returncode not in (0, None) else ""
            res = {"case_id": row["case_id"], "status": "failed",
                   "error": f"processing stopped unexpectedly{hint}", "log_tail": tail, "flags": []}
            status = "failed"
        s.x("UPDATE cases SET status=?, result_json=?, finished_at=?, seconds=?, current_step='' WHERE id=?",
            (status, json.dumps(res), now(), round(time.time() - t0, 1), row["id"]))

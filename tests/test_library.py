"""Scan library: store studies without measuring, measure picked ones in order, stop, delete."""
import json
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from fastapi.testclient import TestClient

from liverhu.web import app as appmod
from liverhu.web import auth
from liverhu.web.app import create_app
from tests.helpers import write_series

PW = "correct-horse-1"
A = ("maria", PW)


@pytest.fixture()
def client(tmp_path):
    (tmp_path / "data").mkdir()
    auth.set_password(tmp_path / "data", PW)
    app = create_app(tmp_path / "data", start_worker=False)
    with TestClient(app) as c:
        yield c, app


def _nifti_bytes(tmp_path, name, nz=24):
    p = tmp_path / name
    nib.save(nib.Nifti1Image(np.zeros((8, 8, nz), np.int16), np.diag([0.8, 0.8, 2.5, 1])), p)
    return p.read_bytes()


def _upload(c, app, files, library=True, label="cohort A"):
    data = {"label": label, "library": "on"} if library else {"label": label}
    jid = c.post("/api/jobs", data=data, auth=A).json()["job_id"]
    assert c.post(f"/api/jobs/{jid}/files", files=files, auth=A).status_code == 200
    assert c.post(f"/api/jobs/{jid}/start", auth=A).json()["ok"]
    assert app.state.intake.intake_once()             # find the studies (normally a background thread)
    return jid


def _fake_measure(app, order):
    """Stand-in for the AI step: records the order and writes a result."""
    store = app.state.store

    def run(jid, row):
        order.append(row["case_id"])
        settings = json.loads((store.case_dir(jid, row["id"]) / "case.json").read_text())["settings"]
        res = {"case_id": row["case_id"], "status": "ok", "liver_mean_hu_3roi": 55.0, "spleen_mean_hu": 48.0,
               "rois": [{"key": "LL", "mean_hu": 55.0, "diameter_mm": settings["roi_diameter_mm"]}], "flags": []}
        store.x("UPDATE cases SET status='ok', result_json=?, finished_at='2026-01-01T00:00:00' WHERE id=?",
                (json.dumps(res), row["id"]))
    app.state.worker._run_case = run


def test_store_then_measure_picked_studies_in_order(client, tmp_path):
    c, app = client
    files = [("files", ("p1/scan_a.nii.gz", _nifti_bytes(tmp_path, "a.nii.gz"))),
             ("files", ("p2/scan_b.nii.gz", _nifti_bytes(tmp_path, "b.nii.gz", nz=30)))]
    jid = _upload(c, app, files)
    store = app.state.store
    assert store.q("SELECT status FROM jobs WHERE id=?", (jid,), one=True)["status"] == "stored"
    lib = c.get("/api/library", auth=A).json()
    by = {s["name"]: s for s in lib["studies"]}
    assert set(by) == {"cohort_A_p1_scan_a", "cohort_A_p2_scan_b"}
    assert all(s["status"] == "stored" for s in by.values())
    assert by["cohort_A_p2_scan_b"]["info"]["n_images"] == 30 and by["cohort_A_p2_scan_b"]["info"]["slice_mm"] == 2.5
    assert lib["stored_bytes"] > 0 and lib["free_bytes"] > 0
    assert c.get("/api/jobs", auth=A).json()["jobs"] == []          # not in the "measure now" worklist
    assert app.state.worker.next_case() is None                       # nothing measured until asked

    a, b = by["cohort_A_p1_scan_a"]["id"], by["cohort_A_p2_scan_b"]["id"]
    r = c.post("/api/library/measure", json={"ids": [b, a], "roi_diameter_mm": 20}, auth=A).json()
    assert r["queued"] == [b, a]
    lib = {s["id"]: s for s in c.get("/api/library", auth=A).json()["studies"]}
    assert lib[b]["status"] == "queued" and lib[b]["place"] == 1 and lib[a]["place"] == 2
    # asking again while waiting does not queue twice
    assert c.post("/api/library/measure", json={"ids": [a]}, auth=A).json()["queued"] == []

    order = []
    _fake_measure(app, order)
    w = app.state.worker
    assert w.run_next() and w.run_next() and not w.run_next()
    assert order == ["cohort_A_p2_scan_b", "cohort_A_p1_scan_a"]             # the order they were picked
    lib = {s["id"]: s for s in c.get("/api/library", auth=A).json()["studies"]}
    assert lib[a]["measured"] and lib[a]["liver_mean"] == 55.0
    res = json.loads(store.q("SELECT result_json FROM cases WHERE id=?", (a,), one=True)["result_json"])
    assert res["rois"][0]["diameter_mm"] == 20.0                       # settings from the library page
    assert any(store.upload_dir(jid).rglob("*.nii.gz"))                # scans are kept in the library
    assert "LL_mean_HU" in c.get("/download/results.csv", auth=A).text


def test_stop_waiting_and_delete(client, tmp_path):
    c, app = client
    files = [("files", ("p1/scan_a.nii.gz", _nifti_bytes(tmp_path, "a.nii.gz"))),
             ("files", ("p2/scan_b.nii.gz", _nifti_bytes(tmp_path, "b.nii.gz")))]
    jid = _upload(c, app, files)
    store = app.state.store
    ids = [s["id"] for s in c.get("/api/library", auth=A).json()["studies"]]
    c.post("/api/library/measure", json={"ids": ids}, auth=A)
    assert c.post("/api/library/unqueue", json={"ids": ids}, auth=A).json()["unqueued"] == ids
    assert {s["status"] for s in c.get("/api/library", auth=A).json()["studies"]} == {"stored"}

    up = store.upload_dir(jid)
    first = ids[0]
    own = json.loads((store.case_dir(jid, first) / "case.json").read_text())["case"]["path"]
    assert c.post("/api/library/delete", json={"ids": [first]}, auth=A).json()["deleted"] == [first]
    left = [str(p.resolve()) for p in up.rglob("*.nii.gz")]
    assert len(left) == 1 and str(Path(own).resolve()) not in left              # its scan file is gone
    assert not Path(own).parent.exists()                                       # and its empty folder
    assert not store.case_dir(jid, first).exists()
    c.post("/api/library/delete", json={"ids": [ids[1]]}, auth=A)
    assert not store.job_dir(jid).exists()                             # empty batch removed entirely
    assert c.get("/api/library", auth=A).json()["studies"] == []


def test_same_study_uploaded_twice_is_pointed_out(client, tmp_path):
    c, app = client
    vol = np.full((10, 10, 24), 40.0)
    for folder in ("first", "second"):
        write_series(vol, (0.8, 0.8, 2.5), tmp_path / folder / "s1", study_uid="1.2.3.4.5")
    files = [("files", (f"{f}/s1/{p.name}", p.read_bytes())) for f in ("first", "second")
             for p in sorted((tmp_path / f / "s1").iterdir())]
    _upload(c, app, files[:24], label="")
    _upload(c, app, files[24:], label="")
    studies = c.get("/api/library", auth=A).json()["studies"]
    assert len(studies) == 2
    assert studies[0]["same_study_as"] == [studies[1]["id"]] and studies[1]["same_study_as"] == [studies[0]["id"]]
    assert studies[0]["info"]["n_images"] == 24 and "patient_id" not in studies[0]["info"]


def test_measure_now_uploads_still_work_and_clean_up(client, tmp_path):
    c, app = client
    jid = _upload(c, app, [("files", ("x.nii.gz", _nifti_bytes(tmp_path, "x.nii.gz")))], library=False)
    store = app.state.store
    assert store.q("SELECT status FROM jobs WHERE id=?", (jid,), one=True)["status"] == "running"
    order = []
    _fake_measure(app, order)
    assert app.state.worker.run_next()
    job = store.q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
    assert job["status"] == "done" and job["n_done"] == 1
    assert not store.upload_dir(jid).exists()                          # scans deleted after measuring
    # library actions do not touch "measure now" studies
    cid = store.q("SELECT id FROM cases WHERE job_id=?", (jid,), one=True)["id"]
    assert c.post("/api/library/delete", json={"ids": [cid]}, auth=A).json()["deleted"] == []


def test_upload_refused_when_disk_is_nearly_full(client, monkeypatch, tmp_path):
    c, _ = client
    jid = c.post("/api/jobs", data={"library": "on"}, auth=A).json()["job_id"]
    monkeypatch.setattr(appmod, "MIN_FREE_BYTES", 1 << 62)
    r = c.post(f"/api/jobs/{jid}/files", files=[("files", ("x.nii.gz", b"123"))], auth=A)
    assert r.status_code == 507 and "disk space" in r.text


def test_library_page_renders(client):
    c, _ = client
    r = c.get("/library", auth=A)
    assert r.status_code == 200 and "Scan library" in r.text and 'aria-current="page">Library' in r.text


def test_older_database_is_upgraded(tmp_path):
    import sqlite3
    from liverhu.web.jobs import Store
    db = tmp_path / "liverhu.sqlite"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, submitted_by TEXT,"
                      " created_at TEXT, status TEXT, n_cases INTEGER DEFAULT 0, n_done INTEGER DEFAULT 0,"
                      " n_files INTEGER DEFAULT 0, message TEXT DEFAULT '', settings_json TEXT,"
                      " include_identifiers INTEGER DEFAULT 0);"
                      "CREATE TABLE cases (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, case_id TEXT,"
                      " status TEXT, result_json TEXT, started_at TEXT, finished_at TEXT, seconds REAL,"
                      " current_step TEXT DEFAULT '');"
                      "INSERT INTO jobs (label, status) VALUES ('old', 'running');"
                      "INSERT INTO cases (job_id, case_id, status) VALUES (1, 'a', 'running');")
    con.commit()
    con.close()
    s = Store(tmp_path)
    assert s.q("SELECT library FROM jobs", one=True)["library"] == 0
    assert s.q("SELECT status, queue_order FROM cases", one=True) == {"status": "queued", "queue_order": None}

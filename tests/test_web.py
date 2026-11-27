import json

import pytest
from fastapi.testclient import TestClient

from liverhu.web import auth
from liverhu.web.app import _safe_rel, create_app

PW = "correct-horse-1"


@pytest.fixture()
def client(tmp_path):
    auth.set_password(tmp_path, PW)
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as c:
        yield c, app


def test_password_required(client):
    c, _ = client
    assert c.get("/").status_code == 401
    assert c.get("/", auth=("maria", "wrong-password")).status_code == 401
    r = c.get("/", auth=("maria", PW))
    assert r.status_code == 200 and "Measure a study" in r.text and "HUnter" in r.text


def test_upload_flow_and_downloads(client, tmp_path):
    c, app = client
    a = ("maria", PW)
    jid = c.post("/api/jobs", data={"label": "cohort A", "roi_diameter_mm": "12"}, auth=a).json()["job_id"]
    files = [("files", ("PatientA/S1/IM00001", b"abc")), ("files", ("../../evil.txt", b"x")),
             ("files", ("PatientA/S1/IM00001", b"same name, different file"))]
    assert c.post(f"/api/jobs/{jid}/files", files=files, auth=a).json()["saved"] == 3
    up = app.state.store.upload_dir(jid)
    saved = sorted(str(p.relative_to(up)).replace("\\", "/") for p in up.rglob("*") if p.is_file())
    assert saved == ["PatientA/S1/IM00001", "PatientA/S1/IM00001__2", "evil.txt"]
    assert not (tmp_path.parent / "evil.txt").exists()
    assert c.post(f"/api/jobs/{jid}/start", auth=a).json()["ok"]
    job = c.get(f"/api/jobs/{jid}", auth=a).json()["job"]
    assert job["status"] == "queued" and job["submitted_by"] == "maria" and job["label"] == "cohort_A"
    s = json.loads(app.state.store.q("SELECT settings_json FROM jobs WHERE id=?", (jid,), one=True)["settings_json"])
    assert s["roi_diameter_mm"] == 12.0
    # closed upload refuses more files
    assert c.post(f"/api/jobs/{jid}/files", files=[("files", ("x", b"1"))], auth=a).status_code == 409
    # fake a finished case and check the spreadsheet download
    store = app.state.store
    res = {"case_id": "PatientA", "status": "ok", "rois": [{"key": "LL", "mean_hu": 51.234, "sd_hu": 9.0}],
           "flags": ["test flag"], "input": {"series_description": "ABD"}}
    store.x("INSERT INTO cases (job_id, case_id, status, result_json) VALUES (?,?,?,?)",
            (jid, "PatientA", "ok", json.dumps(res)))
    csv = c.get(f"/download/results.csv?job={jid}", auth=a).text
    assert "LL_mean_HU" in csv and "51.2" in csv and "test flag" in csv
    assert c.get("/download/results.xlsx", auth=a).status_code == 200
    assert c.post(f"/api/jobs/{jid}/delete", auth=a).json()["ok"]
    assert c.get(f"/api/jobs/{jid}", auth=a).status_code == 404


def test_safe_rel():
    assert str(_safe_rel("../../a/../b//c")).replace("\\", "/") == "a/b/c"
    assert str(_safe_rel("")) == "file"


def test_short_password_rejected(tmp_path):
    with pytest.raises(ValueError):
        auth.set_password(tmp_path, "short")


def test_https_certificate_is_created_once_and_loads(tmp_path):
    import ssl
    from liverhu.web import tls
    cert, key = tls.ensure_cert(tmp_path)
    assert cert.exists() and key.exists()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))          # valid pair, usable by the server
    before = cert.read_bytes()
    assert tls.ensure_cert(tmp_path)[0].read_bytes() == before   # reused, not regenerated


def test_export_page_and_outliers(client):
    c, app = client
    a = ("maria", PW)
    assert 'href="/export"' in c.get("/", auth=a).text and "/download/results.xlsx" not in c.get("/", auth=a).text
    r = c.get("/export", auth=a)
    assert r.status_code == 200 and "Export to Excel" in r.text and 'aria-current="page">Export' in r.text
    store = app.state.store
    jid = c.post("/api/jobs", data={"label": "cohort"}, auth=a).json()["job_id"]
    values = [55.0, 57.0, 58.0, 60.0, 61.0, 59.0, 95.0, 120.0]      # 95 = outlier among the non-contrast
    for i, v in enumerate(values):
        phase = "contrast-enhanced" if v == 120.0 else "non-contrast"
        res = {"case_id": f"p{i}", "status": "ok", "liver_mean_hu_3roi": v, "contrast_phase": phase,
               "rois": [{"key": "LL", "mean_hu": v}], "flags": []}
        store.x("INSERT INTO cases (job_id, case_id, status, result_json, finished_at) VALUES (?,?,?,?,?)",
                (jid, f"p{i}", "ok", json.dumps(res), f"2026-01-01T00:00:0{i}"))
    store.x("INSERT INTO cases (job_id, case_id, status, result_json) VALUES (?,?,?,?)",
            (jid, "bad", "failed", json.dumps({"case_id": "bad", "status": "failed", "error": "x"})))
    d = c.get("/api/export", auth=a).json()
    assert [s["n"] for s in d["scans"]] == list(range(1, 9))              # failed scans are not plotted
    out = {s["name"]: s["outlier"] for s in d["scans"]}
    assert out["p6"] == "high" and out["p7"] == "" and sum(bool(v) for v in out.values()) == 1
    assert d["groups"]["non-contrast"]["n"] == 7 and d["groups"]["non-contrast"]["fences"]["median"] == 59.0
    assert d["groups"]["contrast-enhanced"]["fences"] is None              # too few to judge
    assert d["batches"] == [{"id": jid, "label": "cohort", "n": 8}]
    assert c.get(f"/api/export?job={jid + 1}", auth=a).json()["scans"] == []
    csv = c.get("/download/results.csv", auth=a).text
    head = csv.splitlines()[0].split(",")
    assert head[head.index("liver_mean_HU_3ROI") + 1] == "liver_mean_outlier"
    assert "HIGH" in [line.split(",")[head.index("liver_mean_outlier")] for line in csv.splitlines()[1:]]


def test_outlier_rule():
    from liverhu.outliers import fences, mark_outliers
    f = fences([10, 10, 10, 10, 10])
    assert f["low"] == 10 - 1.5 * 5 and f["high"] == 10 + 1.5 * 5               # minimum spread of 5 HU
    items = [{"v": v, "p": "non-contrast"} for v in (50, 52, 54, 56, 58, 20)] + [{"v": None, "p": None}]
    g = mark_outliers(items, "v", "p")
    assert items[5]["outlier"] == "low" and items[6]["outlier"] == "" and set(g) == {"non-contrast", "uncertain"}

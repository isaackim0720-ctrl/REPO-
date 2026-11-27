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
    assert r.status_code == 200 and "Measure liver HU" in r.text


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

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from liverhu import edit
from liverhu.config import Settings
from liverhu.pipeline import flatten
from liverhu.roi import place_rois_with_stats, place_spleen_roi
from tests.test_roi import make_phantom

SPACING = (0.8, 0.8, 2.5)


def _case(tmp_path):
    ct, seg, aff, _ = make_phantom(seed=21)
    X, Y, Z = np.meshgrid(*(np.arange(n) for n in ct.shape), indexing="ij")
    spleen = ((X - 25) / 14.0) ** 2 + ((Y - 30) / 20.0) ** 2 + ((Z - 40) / 15.0) ** 2 <= 1
    spleen &= seg == 0
    ct[spleen] = 45.0
    rois, _ = place_rois_with_stats(ct, seg, SPACING, aff, Settings())
    spl, _ = place_spleen_roi(ct, spleen, SPACING, aff, Settings())
    result = {"case_id": "c1", "status": "ok", "rois": [r.as_dict() for r in rois] + [spl.as_dict()],
              "flags": ["LL SD 30 HU is high - check image", "Contrast agent recorded in DICOM"]}
    edit.recompute_summary(result)
    edit.save_edit_data(tmp_path, ct, seg, spleen, aff, SPACING, slice_positions=[[k * 2.5, k + 1] for k in range(ct.shape[2])])
    return ct, seg, spleen, aff, rois, spl, result


def test_editor_measure_matches_automatic_exactly(tmp_path):
    _, _, _, _, rois, spl, _ = _case(tmp_path)
    ed = edit.EditData(tmp_path)
    for r in rois + [spl]:
        x, y, z = ed.to_crop(r.center_voxel)
        m = edit.measure_at(ed, r.key, x, y, z, r.diameter_mm)
        assert m.center_voxel == tuple(r.center_voxel)
        for f in ("mean_hu", "sd_hu", "min_hu", "max_hu", "area_cm2"):
            assert getattr(m, f) == pytest.approx(getattr(r, f), abs=1e-6), (r.key, f)
        assert m.dicom_instance == r.center_voxel[2] + 1


def test_manual_save_reset_and_spreadsheet(tmp_path):
    _, _, _, _, rois, _, result = _case(tmp_path)
    ed = edit.EditData(tmp_path)
    ll = next(r for r in rois if r.key == "LL")
    x, y, z = ed.to_crop(ll.center_voxel)
    moved = edit.measure_at(ed, "LL", x + 3, y - 2, z, 20.0)
    new = edit.apply_manual(result, moved, "maria")
    r = next(r for r in new["rois"] if r["key"] == "LL")
    assert r["manual"] and r["edited_by"] == "maria" and r["diameter_mm"] == 20.0
    assert r["auto"]["mean_hu"] == pytest.approx(ll.mean_hu) and r["auto"]["diameter_mm"] == 15.0
    assert any("LL moved by hand by maria" in f for f in new["flags"])
    assert not any(f.startswith("LL SD 30") for f in new["flags"])          # reviewed -> old LL flag cleared
    assert "Contrast agent recorded in DICOM" in new["flags"]
    others = [q["mean_hu"] for q in new["rois"] if q["key"] in ("RL1", "RL2")]
    assert new["liver_mean_hu_3roi"] == pytest.approx(np.mean([moved.mean_hu] + others))
    assert new["liver_minus_spleen_hu"] == pytest.approx(new["liver_mean_hu_3roi"] - new["spleen_mean_hu"])
    assert flatten(new)["LL_manual"] == "yes, by maria" and flatten(new)["RL1_manual"] == ""
    back = edit.reset_to_auto(new, "LL")
    rb = next(r for r in back["rois"] if r["key"] == "LL")
    assert not rb.get("manual") and rb["mean_hu"] == pytest.approx(ll.mean_hu)
    assert back["liver_mean_hu_3roi"] == pytest.approx(result["liver_mean_hu_3roi"])
    assert "LL SD 30 HU is high - check image" in back["flags"]
    assert not any("moved by hand" in f for f in back["flags"])


def test_check_circle_warns_outside_and_wrong_side(tmp_path):
    _, seg, _, _, _, _, _ = _case(tmp_path)
    ed = edit.EditData(tmp_path)
    xs, ys = np.nonzero(ed.seg[:, :, ed.seg.shape[2] // 2] == 7) if (ed.seg == 7).any() else (None, None)
    z = int(np.nonzero((ed.seg == 7).any(axis=(0, 1)))[0].mean())
    xs, ys = np.nonzero(ed.seg[:, :, z] == 7)
    c = edit.check_circle(ed, "LL", int(np.median(xs)), int(np.median(ys)), z, 15.0)
    assert any("not left of the falciform ligament" in w for w in c["warnings"])
    c2 = edit.check_circle(ed, "LL", 12, 12, z, 15.0)        # corner of the crop: outside the liver
    assert c2["inside_fraction"] < 1 and any("outside the liver" in w for w in c2["warnings"])


def test_slice_png_has_display_size(tmp_path):
    _case(tmp_path)
    ed = edit.EditData(tmp_path)
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(edit.slice_png(ed, 10)))
    assert im.size == (ed.ct.shape[0], ed.ct.shape[1])        # width = x, height = y


def test_editor_web_api(tmp_path):
    from liverhu.web import auth
    from liverhu.web.app import create_app
    auth.set_password(tmp_path, "pw-12345678")
    app = create_app(tmp_path, start_worker=False)
    store = app.state.store
    a = ("maria", "pw-12345678")
    with TestClient(app) as c:
        jid = c.post("/api/jobs", data={"label": "t"}, auth=a).json()["job_id"]
        cid = store.x("INSERT INTO cases (job_id, case_id, status) VALUES (?,?,?)", (jid, "c1", "ok"))
        d = store.case_dir(jid, cid)
        d.mkdir(parents=True, exist_ok=True)
        _, _, _, _, rois, _, result = _case(d)
        result["editable"] = True
        store.x("UPDATE cases SET result_json=? WHERE id=?", (json.dumps(result), cid))
        info = c.get(f"/api/cases/{cid}/edit-info", auth=a).json()
        assert info["available"] and [r["key"] for r in info["rois"]] == ["LL", "RL1", "RL2", "SPL"]
        ll = info["rois"][0]
        m = c.get(f"/api/cases/{cid}/measure", params={"key": "LL", "x": ll["x"], "y": ll["y"], "z": ll["z"], "d": 15}, auth=a).json()
        assert m["mean_hu"] == pytest.approx(rois[0].mean_hu)
        assert c.get(f"/cases/{cid}/slice/{ll['z']}.png", auth=a).headers["content-type"] == "image/png"
        assert c.get(f"/cases/{cid}/edit", auth=a).status_code == 200
        s = c.post(f"/api/cases/{cid}/rois/LL", json={"x": ll["x"] + 2, "y": ll["y"], "z": ll["z"], "d": 12}, auth=a).json()
        assert s["ok"] and (d / "qa.png").exists()
        saved = json.loads(store.q("SELECT result_json FROM cases WHERE id=?", (cid,), one=True)["result_json"])
        assert next(r for r in saved["rois"] if r["key"] == "LL")["manual"]
        summary = c.get(f"/api/jobs/{jid}", auth=a).json()["cases"][0]
        assert summary["LL"]["manual"] and summary["revision"] == 1 and summary["editable"]
        assert c.post(f"/api/cases/{cid}/rois/LL/reset", auth=a).json()["ok"]
        back = json.loads(store.q("SELECT result_json FROM cases WHERE id=?", (cid,), one=True)["result_json"])
        assert not next(r for r in back["rois"] if r["key"] == "LL").get("manual")
        assert c.get(f"/api/cases/{cid}/measure", params={"key": "XX", "x": 1, "y": 1, "z": 1, "d": 15}, auth=a).status_code == 404

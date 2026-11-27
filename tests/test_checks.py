import numpy as np
import pytest

from liverhu import checks, edit
from liverhu.config import Settings
from liverhu.pipeline import flatten
from liverhu.roi import RoiResult

S = Settings()


def test_contrast_phase_from_aorta_tag_and_spleen():
    assert checks.contrast_phase(45.0, "", None, S)[0] == checks.PHASE_NON
    assert checks.contrast_phase(180.0, "", None, S)[0] == checks.PHASE_CON
    assert checks.contrast_phase(110.0, "", None, S)[0] == checks.PHASE_UNK
    assert checks.contrast_phase(45.0, "OMNIPAQUE", None, S)[0] == checks.PHASE_UNK   # conflicting evidence
    assert checks.contrast_phase(None, "OMNIPAQUE", None, S)[0] == checks.PHASE_CON
    assert checks.contrast_phase(None, "", 120.0, S)[0] == checks.PHASE_CON
    assert checks.contrast_phase(None, "", 48.0, S)[0] == checks.PHASE_NON
    assert checks.contrast_phase(None, "", None, S)[0] == checks.PHASE_UNK


def test_hu_ranges_and_checks():
    non = checks.hu_ranges(checks.PHASE_NON, S)
    assert non["LL"] == [40.0, 80.0] and non["SPL"] == [30.0, 65.0]
    con = checks.hu_ranges(checks.PHASE_CON, S)
    assert con["RL1"] == [60.0, 170.0]
    unk = checks.hu_ranges(checks.PHASE_UNK, S)
    assert unk["RL2"] == [40.0, 170.0]
    assert checks.check_hu(32.0, non["LL"]) == "low"
    assert checks.check_hu(95.0, non["LL"]) == "high"
    assert checks.check_hu(58.0, non["LL"]) == ""
    assert checks.check_hu(95.0, con["LL"]) == ""


def _roi(key, mean, sd=12.0, **kw):
    return dict(key=key, name=key, placed=True, mean_hu=mean, sd_hu=sd, min_hu=mean - 20, max_hu=mean + 20,
                area_cm2=1.77, diameter_mm=15.0, center_voxel=[1, 1, 1], **kw)


def test_apply_hu_checks_marks_and_flags_and_flatten():
    res = {"rois": [_roi("LL", 32.0), _roi("RL1", 58.0), _roi("RL2", 85.0), _roi("SPL", 47.0)],
           "contrast_phase": checks.PHASE_NON, "hu_ranges": checks.hu_ranges(checks.PHASE_NON, S),
           "flags": ["something else"], "quality": "check", "quality_notes": ["noisy image (SD 27 HU in the circles)"],
           "scan_complete": False, "completeness_notes": ["liver is cut off at the top of the scan"]}
    checks.apply_hu_checks(res)
    by = {r["key"]: r["hu_check"] for r in res["rois"]}
    assert by == {"LL": "low", "RL1": "", "RL2": "high", "SPL": ""}
    assert any("LL mean HU is LOW" in f for f in res["flags"]) and any("RL2 mean HU is HIGH" in f for f in res["flags"])
    assert "something else" in res["flags"]
    checks.apply_hu_checks(res)                       # idempotent: no duplicate HU flags
    assert sum("mean HU is" in f for f in res["flags"]) == 2
    row = flatten(res)
    assert row["LL_HU_check"] == "LOW" and row["RL2_HU_check"] == "HIGH" and row["RL1_HU_check"] == ""
    assert row["quality"] == "check" and row["scan_complete"] == "NO"
    assert "cut off at the top" in row["completeness_notes"] and "noisy" in row["quality_notes"]


def test_completeness_detects_cut_off_and_missing_slices():
    seg = np.zeros((60, 60, 30), np.uint8)
    seg[20:40, 20:40, 5:25] = 5
    ok, notes = checks.completeness(seg, None, [[float(z), z + 1] for z in range(30)])
    assert ok and notes == []
    top = seg.copy(); top[20:40, 20:40, 25:30] = 5
    ok, notes = checks.completeness(top, None)
    assert not ok and any("top of the scan" in n for n in notes)
    side = seg.copy(); side[0:40, 20:40, 5:25] = 5
    ok, notes = checks.completeness(side, None)
    assert not ok and any("side of the image" in n for n in notes)
    gaps = [[float(z), z + 1] for z in range(30) if z not in (12, 13)]
    ok, notes = checks.completeness(seg, None, gaps)
    assert not ok and any("2 slice(s) missing" in n for n in notes)
    spleen = np.zeros_like(seg, bool); spleen[45:59, 45:59, 0:10] = True
    ok, notes = checks.completeness(seg, spleen)
    assert ok and any("spleen is partly outside" in n for n in notes)


def test_quality_levels():
    good = [RoiResult(k, k, True, mean_hu=55.0, sd_hu=12.0) for k in ("LL", "RL1", "RL2")]
    assert checks.quality(good, {"slice_thickness_mm": 3.0}, {"liver_outlier_fraction": 0.05}, 1500, S)[0] == "good"
    noisy = [RoiResult(k, k, True, mean_hu=55.0, sd_hu=30.0) for k in ("LL", "RL1", "RL2")]
    lvl, notes, noise = checks.quality(noisy, {}, {}, 1500, S)
    assert lvl == "check" and noise == pytest.approx(30.0) and "noisy" in notes[0]
    very = [RoiResult(k, k, True, mean_hu=55.0, sd_hu=45.0) for k in ("LL", "RL1", "RL2")]
    assert checks.quality(very, {}, {}, 1500, S)[0] == "poor"
    assert checks.quality(good, {"warning_not_hu": "x"}, {}, 1500, S)[0] == "poor"
    assert checks.quality(good, {"slice_thickness_mm": 7.5}, {}, 1500, S)[0] == "check"
    assert checks.quality(good, {}, {"liver_outlier_fraction": 0.5}, 1500, S)[0] == "check"
    missing = good[:2] + [RoiResult("RL2", "RL2", False)]
    assert checks.quality(missing, {}, {}, 1500, S)[0] == "poor"


def test_manual_edit_rechecks_hu():
    res = {"rois": [_roi("LL", 55.0), _roi("RL1", 58.0), _roi("RL2", 60.0)],
           "contrast_phase": checks.PHASE_NON, "hu_ranges": checks.hu_ranges(checks.PHASE_NON, S), "flags": []}
    checks.apply_hu_checks(res)
    moved = RoiResult("LL", "Left lobe", True, mean_hu=25.0, sd_hu=10.0, diameter_mm=15.0, center_voxel=(1, 1, 1))
    new = edit.apply_manual(res, moved, "maria")
    assert next(r for r in new["rois"] if r["key"] == "LL")["hu_check"] == "low"
    assert any("LL mean HU is LOW" in f for f in new["flags"])
    back = edit.reset_to_auto(new, "LL")
    assert next(r for r in back["rois"] if r["key"] == "LL")["hu_check"] == ""
    assert not any("LL mean HU is LOW" in f for f in back["flags"])

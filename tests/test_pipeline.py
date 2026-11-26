from liverhu.pipeline import _flags, flatten
from liverhu.roi import RoiResult


def _roi(key, mean, **kw):
    return dict(key=key, placed=True, mean_hu=mean, sd_hu=10.0, min_hu=mean - 20, max_hu=mean + 20,
                area_cm2=1.77, diameter_mm=15.0, z_mm=0.0, dicom_instance=None, segment_at_center=0,
                side_of_ligament="n/a", **kw)


def test_spreadsheet_has_spleen_and_liver_spleen_metrics():
    res = {"case_id": "c1", "status": "ok",
           "rois": [_roi("LL", 50.0), _roi("RL1", 54.0), _roi("RL2", 52.0), _roi("SPL", 48.0)],
           "liver_mean_hu_3roi": 52.0, "spleen_mean_hu": 48.0,
           "liver_minus_spleen_hu": 4.0, "liver_spleen_ratio": 52.0 / 48.0, "flags": []}
    row = flatten(res)
    assert row["SPL_mean_HU"] == 48.0 and row["SPL_SD_HU"] == 10.0
    assert row["SPL_min_HU"] == 28 and row["SPL_max_HU"] == 68 and row["SPL_area_cm2"] == 1.77
    assert row["spleen_mean_HU"] == 48.0
    assert row["liver_minus_spleen_HU"] == 4.0
    assert row["liver_spleen_ratio"] == round(52 / 48, 3)


def test_old_results_without_spleen_still_flatten():
    row = flatten({"case_id": "old", "status": "ok", "rois": [_roi("LL", 50.0)], "flags": []})
    assert row["SPL_mean_HU"] is None and row["liver_spleen_ratio"] is None


def test_spleen_flags():
    missing = RoiResult("SPL", "Spleen", False, "spleen not found in scan")
    flags = _flags([], {}, False, 1500.0, {}, missing, {})
    assert any("Spleen not measured" in f for f in flags)
    noisy = RoiResult("SPL", "Spleen", True, mean_hu=45.0, sd_hu=30.0, side_ok=True)
    flags = _flags([], {}, False, 1500.0, {}, noisy, {"spleen_typical_hu": 45.0})
    assert any("SPL SD 30" in f for f in flags)

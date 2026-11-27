import math

import numpy as np
import pytest

from liverhu.config import Settings
from liverhu.roi import disk_kernel, ligament_points, place_rois, place_rois_with_stats

# Region HU values in the phantom (each ROI must recover its own region's value)
HU_LL, HU_S4, HU_TOP, HU_BOTTOM = 50.0, 55.0, 60.0, 66.0


def make_phantom(spacing=(0.8, 0.8, 2.5), shape=(220, 180, 80), noise=8.0, seed=0):
    """Ellipsoid 'liver' in RAS orientation (x -> patient right, z -> superior)."""
    rng = np.random.default_rng(seed)
    sx, sy, sz = spacing
    X, Y, Z = np.meshgrid(*(np.arange(n) for n in shape), indexing="ij")
    cx, cy, cz = shape[0] / 2, shape[1] / 2, shape[2] / 2
    ax, ay, az = 0.45 * shape[0], 0.45 * shape[1], 0.45 * shape[2]
    liver = ((X - cx) / ax) ** 2 + ((Y - cy) / ay) ** 2 + ((Z - cz) / az) ** 2 <= 1
    x_lig = int(0.30 * shape[0])        # falciform ligament plane (patient-left part = x < x_lig)
    x_s4 = int(0.45 * shape[0])         # segment 4 / right-lobe boundary
    z_mid = int(cz)
    seg = np.zeros(shape, np.uint8)
    seg[liver & (X < x_lig) & (Z >= z_mid)] = 2
    seg[liver & (X < x_lig) & (Z < z_mid)] = 3
    seg[liver & (X >= x_lig) & (X < x_s4)] = 4
    right = liver & (X >= x_s4)
    seg[right & (Z < z_mid) & (Y >= cy)] = 5
    seg[right & (Z < z_mid) & (Y < cy)] = 6
    seg[right & (Z >= z_mid) & (Y < cy)] = 7
    seg[right & (Z >= z_mid) & (Y >= cy)] = 8
    ct = np.full(shape, -100.0, np.float32)            # surrounding fat
    ct[np.isin(seg, (2, 3))] = HU_LL
    ct[seg == 4] = HU_S4
    ct[np.isin(seg, (7, 8))] = HU_TOP
    ct[np.isin(seg, (5, 6))] = HU_BOTTOM
    ct += rng.normal(0, noise, shape).astype(np.float32) * liver
    affine = np.diag([sx, sy, sz, 1.0])
    return ct, seg, affine, dict(x_lig=x_lig, x_s4=x_s4, z_mid=z_mid, liver=liver)


def roi(results, key):
    return next(r for r in results if r.key == key)


def test_one_slice_all_liver_circles_same_slice_front_and_back():
    ct, seg, aff, info = make_phantom()
    res, stats = place_rois_with_stats(ct, seg, (0.8, 0.8, 2.5), aff, Settings())
    ll, rl1, rl2 = roi(res, "LL"), roi(res, "RL1"), roi(res, "RL2")
    assert stats["single_slice"] and all(r.placed for r in res)
    assert ll.center_voxel[2] == rl1.center_voxel[2] == rl2.center_voxel[2] == stats["slice_index"]
    assert set(ll.segments_in_roi) <= {2, 3} and ll.center_voxel[0] < info["x_lig"] and ll.side_ok
    assert set(rl1.segments_in_roi) <= {5, 8} and set(rl2.segments_in_roi) <= {6, 7}   # front / back sectors
    assert rl1.center_voxel[1] > rl2.center_voxel[1]                                      # RL1 above RL2 in the image
    d = math.hypot((rl1.center_voxel[0] - rl2.center_voxel[0]) * 0.8, (rl1.center_voxel[1] - rl2.center_voxel[1]) * 0.8)
    assert d >= Settings().roi_diameter_mm                                                # circles do not overlap
    assert abs(ll.mean_hu - HU_LL) < 4
    assert min(abs(rl1.mean_hu - v) for v in (HU_TOP, HU_BOTTOM)) < 4


def test_spleen_circle_is_the_largest_that_fits():
    from liverhu.roi import place_spleen_roi
    shape, spacing = (200, 160, 60), (0.8, 0.8, 2.5)
    X, Y, Z = np.meshgrid(*(np.arange(n) for n in shape), indexing="ij")
    spleen = ((X - 40) / 30.0) ** 2 + ((Y - 60) / 45.0) ** 2 + ((Z - 30) / 22.0) ** 2 <= 1
    ct = np.where(spleen, 45.0, -100.0).astype(np.float32) + np.random.default_rng(2).normal(0, 5, shape).astype(np.float32) * spleen
    aff = np.diag([*spacing, 1.0])
    r, st = place_spleen_roi(ct, spleen, spacing, aff, Settings(), prefer_z=35)
    # widest part of the spleen: half-width 30 px x 0.8 mm = 24 mm, minus the 5 mm edge gap
    assert st["spleen_largest_circle"] and abs(r.center_voxel[2] - 30) <= 1
    assert 36.0 <= r.diameter_mm <= 38.0 and r.diameter_mm > Settings().roi_diameter_mm
    assert r.edge_distance_mm >= Settings().edge_margin_mm - 1.0
    # the old rule is still available: fixed size, on the liver slice when the spleen is there
    fixed = Settings(spleen_largest_circle=False, roi_diameter_mm=15.0)
    r1, st1 = place_spleen_roi(ct, spleen, spacing, aff, fixed, prefer_z=35)
    assert st1["same_slice"] and r1.center_voxel[2] == 35 and r1.diameter_mm == 15.0
    r2, st2 = place_spleen_roi(ct, spleen, spacing, aff, fixed, prefer_z=59)   # spleen not on that slice
    assert r2.placed and not st2["same_slice"] and r2.center_voxel[2] != 59


def test_three_rois_on_correct_side_of_ligament_and_levels():
    """Separate-slices mode (single_slice=False): RL1 upper (segment 8), RL2 lower."""
    ct, seg, aff, info = make_phantom()
    spacing = (0.8, 0.8, 2.5)
    res = place_rois(ct, seg, spacing, aff, Settings(single_slice=False))
    ll, rl1, rl2 = roi(res, "LL"), roi(res, "RL1"), roi(res, "RL2")
    assert all(r.placed for r in res)
    # Left lobe ROI: inside segments 2/3, patient-left of the ligament (smaller x)
    assert ll.segment_at_center in (2, 3) and set(ll.segments_in_roi) <= {2, 3}
    assert ll.center_voxel[0] < info["x_lig"]
    assert ll.side_of_ligament == "left" and ll.side_ok
    assert ll.ligament_distance_mm >= Settings().ligament_margin_mm - 1.0
    # Right lobe ROIs: right of the ligament, RL1 upper (7/8), RL2 lower (5/6)
    for r in (rl1, rl2):
        assert r.center_voxel[0] >= info["x_s4"] and r.side_of_ligament == "right" and r.side_ok
    assert set(rl1.segments_in_roi) <= {8} and set(rl2.segments_in_roi) <= {5, 6}   # RL1: anterior upper (seg 8)
    assert rl1.z_mm > rl2.z_mm
    assert rl1.z_mm - rl2.z_mm >= Settings().min_rl_separation_mm - 1e-6
    # Values come from the right tissue
    assert abs(ll.mean_hu - HU_LL) < 4
    assert abs(rl1.mean_hu - HU_TOP) < 4
    assert abs(rl2.mean_hu - HU_BOTTOM) < 4
    assert all(r.edge_distance_mm >= Settings().edge_margin_mm - 1.0 for r in res)


def test_statistics_match_direct_computation():
    ct, seg, aff, _ = make_phantom(seed=3)
    s = Settings()
    res = place_rois(ct, seg, (0.8, 0.8, 2.5), aff, s)
    k = disk_kernel(s.roi_diameter_mm / 2, 0.8, 0.8)
    kx, ky = k.shape[0] // 2, k.shape[1] // 2
    for r in res:
        x, y, z = r.center_voxel
        vals = ct[x - kx:x + kx + 1, y - ky:y + ky + 1, z][k].astype(np.float64)
        assert r.n_pixels == vals.size
        assert r.mean_hu == pytest.approx(vals.mean(), abs=1e-6)
        assert r.sd_hu == pytest.approx(vals.std(ddof=1), abs=1e-6)
        assert r.min_hu == pytest.approx(vals.min()) and r.max_hu == pytest.approx(vals.max())
        assert r.area_mm2 == pytest.approx(vals.size * 0.8 * 0.8)
        assert r.area_mm2 == pytest.approx(math.pi * (s.roi_diameter_mm / 2) ** 2, rel=0.05)


def test_avoids_vessel_and_cyst():
    ct, seg, aff, info = make_phantom(seed=5)
    shape = ct.shape
    X, Y = np.meshgrid(np.arange(shape[0]), np.arange(shape[1]), indexing="ij")
    # Bright vessel tube through the whole upper right lobe, and a large homogeneous cyst in LL
    for z in range(shape[2]):
        tube = (np.hypot(X - int(0.72 * shape[0]), Y - int(0.40 * shape[1])) < 6) & np.isin(seg[:, :, z], (7, 8))
        ct[:, :, z][tube] = 160.0
    cyst = (np.hypot(X - int(0.18 * shape[0]), Y - shape[1] // 2) < 18)
    for z in range(shape[2]):
        ct[:, :, z][cyst & np.isin(seg[:, :, z], (2, 3))] = 5.0
    res = place_rois(ct, seg, (0.8, 0.8, 2.5), aff, Settings())
    assert all(r.max_hu < 120 for r in res)
    res2 = place_rois(ct, seg, (0.8, 0.8, 2.5), aff, Settings(single_slice=False))
    assert abs(roi(res2, "RL1").mean_hu - HU_TOP) < 4 and roi(res2, "RL1").max_hu < 120
    assert abs(roi(res, "LL").mean_hu - HU_LL) < 4 and roi(res, "LL").min_hu > 15


def test_large_lesion_filling_most_of_region_is_avoided():
    """A big low-density mass fills most of the lower right lobe; only a rim is normal.
    RL2 must measure the normal rim, not the mass (real failure seen on a public CT)."""
    ct, seg, aff, info = make_phantom(seed=7)
    shape = ct.shape
    X, Y = np.meshgrid(np.arange(shape[0]), np.arange(shape[1]), indexing="ij")
    cx, cy = int(0.72 * shape[0]), shape[1] // 2
    for z in range(shape[2]):
        lower = np.isin(seg[:, :, z], (5, 6))
        mass = lower & (np.hypot(X - cx, Y - cy) < 0.33 * shape[0])
        ct[:, :, z][mass] = 20.0 + np.random.default_rng(z).normal(0, 8, mass.sum())
    res = place_rois(ct, seg, (0.8, 0.8, 2.5), aff, Settings(single_slice=False))
    rl2 = roi(res, "RL2")
    assert rl2.placed and abs(rl2.mean_hu - HU_BOTTOM) < 6, rl2.mean_hu
    one = place_rois(ct, seg, (0.8, 0.8, 2.5), aff, Settings())
    assert all(r.placed and r.mean_hu > 40 for r in one)          # no circle inside the 20 HU mass


def test_region_that_differs_everywhere_is_still_measured():
    """Whole left lobe 30 HU lower than the rest (focal fat): LL still placed, value kept."""
    ct, seg, aff, _ = make_phantom(seed=9)
    ll = np.isin(seg, (2, 3))
    ct[ll] -= 40.0
    res = place_rois(ct, seg, (0.8, 0.8, 2.5), aff, Settings())
    r = roi(res, "LL")
    assert r.placed and abs(r.mean_hu - (HU_LL - 40.0)) < 4
    assert r.fallback_level == 4


def test_spleen_roi_inside_spleen_avoiding_hilar_vessel():
    from liverhu.roi import place_spleen_roi
    rng = np.random.default_rng(11)
    shape, spacing = (200, 160, 60), (0.8, 0.8, 2.5)
    X, Y, Z = np.meshgrid(*(np.arange(n) for n in shape), indexing="ij")
    spleen = ((X - 40) / 30.0) ** 2 + ((Y - 60) / 45.0) ** 2 + ((Z - 30) / 22.0) ** 2 <= 1   # patient-left, posterior
    ct = np.full(shape, -100.0, np.float32)
    ct[spleen] = 45.0 + rng.normal(0, 8, spleen.sum())
    hilum = spleen & (np.hypot(X - 55, Y - 75) < 8)        # bright vessel through the spleen
    ct[hilum] = 170.0
    res, stats = place_spleen_roi(ct, spleen, spacing, np.diag([*spacing, 1.0]), Settings())
    assert res.placed and res.key == "SPL" and res.side_of_ligament == "n/a" and res.side_ok
    x, y, z = res.center_voxel
    assert spleen[x, y, z]
    assert abs(res.mean_hu - 45.0) < 4 and res.max_hu < 120
    assert res.edge_distance_mm >= Settings().edge_margin_mm - 1.0
    assert abs(stats["spleen_typical_hu"] - 45.0) < 3


def test_spleen_missing_is_reported_not_crash():
    from liverhu.roi import place_spleen_roi
    res, _ = place_spleen_roi(np.zeros((20, 20, 10), np.float32), None, (1, 1, 1), np.eye(4), Settings())
    assert not res.placed and "not found" in res.reason


def test_anisotropic_pixels_give_true_circle_area():
    k = disk_kernel(7.5, 0.6, 0.9)
    area = k.sum() * 0.6 * 0.9
    assert area == pytest.approx(math.pi * 7.5 ** 2, rel=0.05)


def test_ligament_points_are_between_segments_2_3_and_4():
    _, seg, _, info = make_phantom()
    pts = ligament_points(seg[:, :, seg.shape[2] // 2])
    assert len(pts) > 0
    assert np.all(np.abs(pts[:, 0] - (info["x_lig"] - 1)) <= 1)


def test_missing_liver_reports_not_placed():
    ct = np.zeros((50, 50, 20), np.float32)
    seg = np.zeros((50, 50, 20), np.uint8)
    res = place_rois(ct, seg, (1, 1, 1), np.eye(4), Settings())
    assert [r.placed for r in res] == [False, False, False]


def test_settings_roundtrip():
    s = Settings(roi_diameter_mm=20.0, rl_top_window=(0.5, 0.9))
    s2 = Settings.from_dict(s.to_dict())
    assert s2 == s
    assert Settings.from_dict({"roi_diameter_mm": "12", "save_segmentation": "on"}).roi_diameter_mm == 12.0


def test_right_lobe_segments_are_configurable():
    ct, seg, aff, _ = make_phantom(seed=13)
    res = place_rois(ct, seg, (0.8, 0.8, 2.5), aff, Settings(single_slice=False, rl1_segments=(7,), rl2_segments=(5,)))
    assert roi(res, "RL1").segment_at_center == 7 and set(roi(res, "RL1").segments_in_roi) <= {7}
    assert set(roi(res, "RL2").segments_in_roi) <= {5}
    s2 = Settings.from_dict(Settings(rl1_segments=(8,)).to_dict())
    assert tuple(int(v) for v in s2.rl1_segments) == (8,)


def test_one_slice_positions_follow_the_protocol():
    """30 mm circles near the liver edge; RL1 at the outer right edge, RL2 at the back roughly
    beneath it, with liver between them; the spleen circle is as large as fits."""
    ct, seg, aff, _ = make_phantom(seed=5)
    sp = (0.8, 0.8, 2.5)
    s = Settings()
    res, st = place_rois_with_stats(ct, seg, sp, aff, s)
    ll, r1, r2 = roi(res, "LL"), roi(res, "RL1"), roi(res, "RL2")
    assert st["single_slice"] and st["liver_circle_diameter_mm"] == 30.0
    # (the phantom liver is small: 30 mm circles fit with the half edge gap, level 1)
    assert all(r.diameter_mm == 30.0 and r.fallback_level <= 1 for r in (ll, r1, r2))
    for r in (ll, r1, r2):                       # near the edge, but never closer than the gap
        gap = s.edge_margin_mm / (2 if r.fallback_level else 1)
        assert gap - 0.5 <= r.edge_distance_mm <= gap + 3.0
    z = r1.center_voxel[2]
    rl = np.isin(seg[:, :, z], (5, 6, 7, 8))
    xs, ys = np.nonzero(rl)
    reach = (15.0 + s.edge_margin_mm) / sp[0]     # centre can be no closer to the side than this
    assert r1.center_voxel[0] >= xs.max() - reach - 4          # at the outer right side
    assert r2.center_voxel[1] <= ys.min() + reach + 4          # at the back
    assert r2.center_voxel[1] < r1.center_voxel[1]             # RL2 behind RL1
    dx = abs(r1.center_voxel[0] - r2.center_voxel[0]) * sp[0]
    dist = math.hypot(dx, (r1.center_voxel[1] - r2.center_voxel[1]) * sp[1])
    assert dist >= 30.0 + s.min_circle_gap_mm                  # no overlap, liver in between
    assert dx <= s.rl2_max_offset_mm + 10.0                    # roughly beneath


def test_liver_circles_shrink_only_when_needed():
    from liverhu.roi import liver_diameters
    assert liver_diameters(Settings()) == [30.0, 25.0, 20.0, 15.0]
    assert liver_diameters(Settings(roi_diameter_mm=15.0)) == [15.0]
    ct, seg, aff, _ = make_phantom(seed=5)
    res, st = place_rois_with_stats(ct, seg, (0.8, 0.8, 2.5), aff, Settings(roi_diameter_mm=200.0))
    assert st["liver_circle_diameter_mm"] < 200.0 and "did not fit" in st["diameter_note"]
    assert all(r.placed and r.diameter_mm == st["liver_circle_diameter_mm"] for r in res)

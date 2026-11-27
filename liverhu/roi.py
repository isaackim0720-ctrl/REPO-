"""Automatic placement of three circular liver ROIs and their HU statistics.

Array convention (everything in this module): nibabel canonical RAS+ orientation.
  axis 0 (x) increases toward the patient's RIGHT
  axis 1 (y) increases toward ANTERIOR
  axis 2 (z) increases toward SUPERIOR
So "left of the falciform ligament" means a *smaller* x index, and "near the top"
means a *larger* z index.

Liver anatomy used (Couinaud segments from the segmentation model):
  * Segments 2 + 3 (left lateral section) lie LEFT of the falciform ligament.
  * Segment 4 lies immediately RIGHT of it; segments 5-8 form the right lobe.
  * The falciform ligament runs in the fissure between segments 2/3 and 4, so it is
    estimated on each axial slice as the interface between those segments.

ROIs (defaults; all distances/windows configurable in Settings):
  LL  - left lobe, inside segments 2/3, left of the ligament
  RL1 - right lobe upper ("near top"), inside segment 8 (anterior; falls back to 7/8)
  RL2 - right lobe lower ("near bottom"), inside segments 5/6
Within each target region the circle is placed on the axial slice/position where the
tissue is most homogeneous (lowest SD, close to the regional median HU), which keeps
it away from vessels, lesions and artefacts, as a careful human reader would.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict

import numpy as np
from scipy import ndimage
from scipy.signal import fftconvolve

from .config import Settings

LEFT_OF_LIGAMENT = (2, 3)
SEGMENT_4 = (4,)
RIGHT_OF_LIGAMENT = (4, 5, 6, 7, 8)
RIGHT_LOBE = (5, 6, 7, 8)


@dataclass(frozen=True)
class RoiSpec:
    key: str
    name: str
    labels: tuple            # preferred Couinaud segments
    fallback_labels: tuple   # used only if nothing valid fits in the preferred ones
    extent_labels: tuple     # structure whose top-bottom extent defines the slice window
    window_attr: str         # Settings attribute holding (low, high) fractions, 0=bottom 1=top
    side: str                # "left" or "right" of the falciform ligament


ROI_SPECS = (
    RoiSpec("LL", "Left lobe", (2, 3), (2, 3), (2, 3), "ll_window", "left"),
    RoiSpec("RL1", "Right lobe 1 (upper)", (8,), (7, 8), RIGHT_LOBE, "rl_top_window", "right"),
    RoiSpec("RL2", "Right lobe 2 (lower)", (5, 6), RIGHT_LOBE, RIGHT_LOBE, "rl_bottom_window", "right"),
)
# The spleen ROI works on a spleen mask (label 1), with no ligament rule.
SPLEEN_SPEC = RoiSpec("SPL", "Spleen", (1,), (1,), (1,), "spleen_window", "none")


@dataclass
class RoiResult:
    key: str
    name: str
    placed: bool
    reason: str = ""
    mean_hu: float = float("nan")
    sd_hu: float = float("nan")
    min_hu: float = float("nan")
    max_hu: float = float("nan")
    median_hu: float = float("nan")
    n_pixels: int = 0
    area_mm2: float = float("nan")
    area_cm2: float = float("nan")
    diameter_mm: float = float("nan")
    center_voxel: tuple = ()          # (x, y, z) indices in the canonical array
    center_ras_mm: tuple = ()         # world coordinates, RAS mm
    z_mm: float = float("nan")        # table position (same as DICOM patient z)
    slice_index: int = -1             # z index in the canonical array
    segment_at_center: int = 0
    segments_in_roi: tuple = ()
    side_of_ligament: str = ""        # "left" / "right" / "unknown"
    side_ok: bool = False
    ligament_distance_mm: float = float("nan")   # circle edge -> estimated ligament
    edge_distance_mm: float = float("nan")       # circle edge -> liver boundary (in-plane)
    fallback_level: int = 0
    region_median_hu: float = float("nan")
    dicom_instance: int | None = None   # DICOM InstanceNumber of the ROI slice (DICOM input)
    manual: bool = False                # True when a person moved/resized this circle
    edited_by: str = ""
    edited_at: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "RoiResult":
        import dataclasses
        names = {f.name for f in dataclasses.fields(cls)}
        kw = {k: v for k, v in d.items() if k in names}
        for k in ("center_voxel", "center_ras_mm", "segments_in_roi"):
            if k in kw and kw[k] is not None:
                kw[k] = tuple(kw[k])
        for k, v in list(kw.items()):
            if v is None and isinstance(getattr(cls, k, None), float):
                kw[k] = float("nan")
        return cls(**kw)

    def as_dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float) and not math.isfinite(v):
                d[k] = None
        return d


def disk_kernel(radius_mm: float, sx: float, sy: float) -> np.ndarray:
    """Boolean disk (true circle in mm) on a pixel grid with spacing sx, sy."""
    nx, ny = int(math.floor(radius_mm / sx)), int(math.floor(radius_mm / sy))
    xs = np.arange(-nx, nx + 1)[:, None] * sx
    ys = np.arange(-ny, ny + 1)[None, :] * sy
    return (xs ** 2 + ys ** 2) <= radius_mm ** 2 + 1e-9


def _box_stats(img: np.ndarray, kernel: np.ndarray):
    """Local mean and SD (ddof=1) of img under kernel centred at every pixel."""
    k = kernel.astype(np.float64)
    n = k.sum()
    s1 = fftconvolve(img, k, mode="same")
    s2 = fftconvolve(img * img, k, mode="same")
    mean = s1 / n
    var = np.maximum((s2 - s1 * s1 / n) / max(n - 1, 1), 0.0)
    return mean, np.sqrt(var)


def _bbox(mask: np.ndarray, pad: tuple):
    idx = np.nonzero(mask)
    lo = [max(int(i.min()) - p, 0) for i, p in zip(idx, pad)]
    hi = [min(int(i.max()) + p + 1, s) for i, p, s in zip(idx, pad, mask.shape)]
    return tuple(slice(a, b) for a, b in zip(lo, hi))


def ligament_points(seg2d: np.ndarray) -> np.ndarray:
    """Pixels of segments 2/3 that touch segment 4 (fallback: 4-8) on one axial slice.

    This interface is where the falciform ligament / umbilical fissure lies.
    Returns an (N, 2) array of (x, y) indices; empty if not visible on this slice.
    """
    left = np.isin(seg2d, LEFT_OF_LIGAMENT)
    if not left.any():
        return np.empty((0, 2), int)
    right = np.isin(seg2d, SEGMENT_4)
    if not right.any():
        right = np.isin(seg2d, RIGHT_OF_LIGAMENT)
    iface = left & ndimage.binary_dilation(right, structure=np.ones((3, 3), bool))
    return np.argwhere(iface)


def _ligament_near(seg: np.ndarray, z: int, max_dz: int) -> tuple[np.ndarray, int]:
    for dz in range(0, max_dz + 1):
        for zz in (z - dz, z + dz):
            if 0 <= zz < seg.shape[2]:
                pts = ligament_points(seg[:, :, zz])
                if len(pts):
                    return pts, zz
    return np.empty((0, 2), int), -1


def _z_window(seg: np.ndarray, labels: tuple, frac: tuple) -> tuple[int, int] | None:
    zs = np.nonzero(np.isin(seg, labels).any(axis=(0, 1)))[0]
    if len(zs) == 0:
        return None
    lo, hi = int(zs.min()), int(zs.max())
    a = lo + frac[0] * (hi - lo)
    b = lo + frac[1] * (hi - lo)
    return int(math.floor(a)), int(math.ceil(b))


def liver_typical_hu(ct, seg, spacing):
    """Typical liver parenchyma HU (histogram mode) and its robust SD, plus the in-plane
    smoothed CT used for outlier detection. Lesions/vessels form tails, not the mode."""
    sx, sy, _ = spacing
    smooth = ndimage.gaussian_filter(ct.astype(np.float32), sigma=(max(1.0 / sx, 0.5), max(1.0 / sy, 0.5), 0))
    liver = seg > 0
    interior = ndimage.binary_erosion(liver, iterations=2)
    vals = smooth[interior if interior.sum() > 1000 else liver]
    if vals.size > 2_000_000:
        vals = vals[:: vals.size // 2_000_000 + 1]
    vals = vals[(vals > -300) & (vals < 500)]
    if vals.size == 0:
        return float("nan"), float("nan"), smooth
    hist, edges = np.histogram(vals, bins=np.arange(-300, 501, 1.0))
    hist = ndimage.gaussian_filter1d(hist.astype(float), 3)
    mode = float(edges[int(np.argmax(hist))] + 0.5)
    near = vals[np.abs(vals - mode) < 60]
    rsd = float(1.4826 * np.median(np.abs(near - mode))) if near.size else 10.0
    return mode, rsd, smooth


def _search(ct, seg, spacing, kernel, radius, labels, side, zrange, edge_margin,
            lig_margin, max_outlier_frac, settings, glob, use_global=True, use_outliers=True):
    """Best circle centre within zrange for region `labels`. Returns dict or None.

    glob = (typical liver HU, its robust SD, smoothed CT). With use_global the circle must
    avoid pixels far from the typical liver HU (vessels, lesions) and is scored against it;
    otherwise (last resort) against the region's own median."""
    sx, sy, _ = spacing
    z0, z1 = max(zrange[0], 0), min(zrange[1], ct.shape[2] - 1)
    if z1 < z0:
        return None
    region3d = np.isin(seg[:, :, z0:z1 + 1], labels)
    if region3d.sum() < kernel.sum():
        return None
    interior = ndimage.binary_erosion(region3d, iterations=2)
    vals = ct[:, :, z0:z1 + 1][interior if interior.sum() > 50 else region3d]
    region_median = float(np.median(vals))
    g_mode, g_rsd, smooth3 = glob
    if not np.isfinite(g_mode):
        use_global = False
    ref = g_mode if use_global else region_median
    thr = max(settings.outlier_min_hu, 3.0 * (g_rsd if np.isfinite(g_rsd) else 10.0))
    other_labels = {"left": RIGHT_OF_LIGAMENT, "right": LEFT_OF_LIGAMENT}.get(side, ())
    best = None
    for z in range(z0, z1 + 1):
        seg2d = seg[:, :, z]
        reg = np.isin(seg2d, labels)
        if reg.sum() < kernel.sum():
            continue
        d_reg = ndimage.distance_transform_edt(reg, sampling=(sx, sy))
        valid = d_reg >= radius + edge_margin
        if not valid.any():
            continue
        other = np.isin(seg2d, other_labels) if other_labels else None
        if other is not None and other.any():
            d_other = ndimage.distance_transform_edt(~other, sampling=(sx, sy))
            valid &= d_other >= radius + lig_margin
            if not valid.any():
                continue
        img = ct[:, :, z].astype(np.float64)
        if use_outliers:
            outlier = (np.abs(smooth3[:, :, z] - ref) > thr).astype(np.float64)
            frac_out = fftconvolve(outlier, kernel.astype(np.float64), mode="same") / kernel.sum()
            valid &= frac_out <= max_outlier_frac
            if not valid.any():
                continue
        mean, sd = _box_stats(img, kernel)
        score = sd + settings.homogeneity_weight * np.abs(mean - ref)
        score = np.where(valid, score, np.inf)
        flat = int(np.argmin(score))
        x, y = np.unravel_index(flat, score.shape)
        s = float(score[x, y])
        if best is None or s < best["score"]:
            best = {"score": s, "x": int(x), "y": int(y), "z": int(z), "ref": region_median}
    return best


def _measure(ct, seg, spacing, affine, kernel, radius, spec, found, fallback_level, settings):
    sx, sy, sz = spacing
    x, y, z = found["x"], found["y"], found["z"]
    kx, ky = kernel.shape[0] // 2, kernel.shape[1] // 2
    sl = ct[x - kx:x + kx + 1, y - ky:y + ky + 1, z]
    sg = seg[x - kx:x + kx + 1, y - ky:y + ky + 1, z]
    vals = sl[kernel].astype(np.float64)
    labs = sorted({int(v) for v in np.unique(sg[kernel]) if v > 0})
    n = int(kernel.sum())
    area = n * sx * sy
    world = affine @ np.array([x, y, z, 1.0])

    liver2d = seg[:, :, z] > 0
    d_liver = ndimage.distance_transform_edt(liver2d, sampling=(sx, sy))
    edge = float(d_liver[x, y] - radius)

    center_label = int(seg[x, y, z])
    lig_dist = float("nan")
    if spec.side == "none":          # spleen: no falciform-ligament rule
        side, side_ok = "n/a", True
        center_label, labs = 0, []
    else:
        pts, _ = _ligament_near(seg, z, max_dz=int(math.ceil(10.0 / sz)))
        side_found = "unknown"
        if len(pts):
            dmm = np.sqrt(((pts[:, 0] - x) * sx) ** 2 + ((pts[:, 1] - y) * sy) ** 2)
            lig_dist = float(dmm.min() - radius)
            near_row = pts[np.abs(pts[:, 1] - y) * sy <= 15.0]
            ref_pts = near_row if len(near_row) else pts
            lig_x = float(np.median(ref_pts[:, 0]))
            side_found = "left" if x < lig_x else "right"
        label_side = "left" if center_label in LEFT_OF_LIGAMENT else (
            "right" if center_label in RIGHT_OF_LIGAMENT else "unknown")
        side = side_found if side_found != "unknown" else label_side
        side_ok = (label_side == spec.side) and (side_found in (spec.side, "unknown"))

    return RoiResult(
        key=spec.key, name=spec.name, placed=True,
        mean_hu=float(vals.mean()), sd_hu=float(vals.std(ddof=1)) if n > 1 else 0.0,
        min_hu=float(vals.min()), max_hu=float(vals.max()), median_hu=float(np.median(vals)),
        n_pixels=n, area_mm2=float(area), area_cm2=float(area / 100.0),
        diameter_mm=float(2 * radius),
        center_voxel=(x, y, z), center_ras_mm=tuple(round(float(v), 2) for v in world[:3]),
        z_mm=round(float(world[2]), 2), slice_index=z,
        segment_at_center=center_label, segments_in_roi=tuple(labs),
        side_of_ligament=side, side_ok=bool(side_ok),
        ligament_distance_mm=lig_dist, edge_distance_mm=edge,
        fallback_level=fallback_level, region_median_hu=found["ref"],
    )


def _specs_for(settings: Settings):
    """ROI specs with the right-lobe segments taken from the settings."""
    import dataclasses
    out = []
    for spec in ROI_SPECS:
        if spec.key == "RL1" and tuple(settings.rl1_segments) != spec.labels:
            spec = dataclasses.replace(spec, labels=tuple(int(v) for v in settings.rl1_segments),
                                       fallback_labels=RIGHT_LOBE)
        elif spec.key == "RL2" and tuple(settings.rl2_segments) != spec.labels:
            spec = dataclasses.replace(spec, labels=tuple(int(v) for v in settings.rl2_segments))
        out.append(spec)
    return out


def place_rois(ct: np.ndarray, seg: np.ndarray, spacing, affine, settings: Settings) -> list[RoiResult]:
    """Place LL, RL1, RL2 circles. ct: HU array, seg: Couinaud labels 0-8 (same shape)."""
    return place_rois_with_stats(ct, seg, spacing, affine, settings)[0]


def place_rois_with_stats(ct, seg, spacing, affine, settings: Settings):
    """Like place_rois, also returns {"liver_typical_hu", "liver_typical_sd"}."""
    if ct.shape != seg.shape:
        raise ValueError(f"CT {ct.shape} and segmentation {seg.shape} differ")
    spacing = tuple(float(s) for s in spacing)
    sx, sy, sz = spacing
    radius = settings.roi_diameter_mm / 2.0
    kernel = disk_kernel(radius, sx, sy)

    liver = seg > 0
    if not liver.any():
        return [RoiResult(s.key, s.name, False, "liver not found in scan") for s in ROI_SPECS], {}
    reach = radius + max(settings.edge_margin_mm, settings.ligament_margin_mm) + 5.0
    crop = _bbox(liver, (int(math.ceil(reach / sx)) + 2, int(math.ceil(reach / sy)) + 2, 1))
    ct_c = np.asarray(ct[crop], dtype=np.float32)
    seg_c = np.asarray(seg[crop], dtype=np.uint8)
    offset = np.array([c.start for c in crop])
    aff_c = affine.copy()
    aff_c[:3, 3] = affine[:3, :3] @ offset + affine[:3, 3]

    g_mode, g_rsd, smooth3 = liver_typical_hu(ct_c, seg_c, spacing)
    glob = (g_mode, g_rsd, smooth3)
    m = settings.max_outlier_fraction
    E, L = settings.edge_margin_mm, settings.ligament_margin_mm
    levels = [  # (labels attr, edge margin, ligament margin, window widen, outlier frac, global ref, outliers)
        ("labels", E, L, 0.0, m, True, True),
        ("labels", E / 2, L / 2, 0.0, m, True, True),
        ("fallback_labels", E / 2, L / 2, 0.10, m, True, True),
        ("fallback_labels", 1.0, 3.0, 1.0, 0.15, True, True),
        # last resort: the whole region differs from the rest of the liver (e.g. focal fat):
        # take its most uniform spot, judged against the region's own median (flagged)
        ("labels", E / 2, L / 2, 0.0, 1.0, False, False),
    ]
    results: list[RoiResult] = []
    rl1_z = None
    for spec in _specs_for(settings):
        found, used_level = None, 0
        for lvl, (lab_attr, edge_m, lig_m, widen, out_frac, use_global, use_out) in enumerate(levels):
            labels = getattr(spec, lab_attr)
            f0, f1 = getattr(settings, spec.window_attr)
            frac = (max(0.0, f0 - widen), min(1.0, f1 + widen)) if widen < 1.0 else (0.05, 0.95)
            zr = _z_window(seg_c, spec.extent_labels, frac)
            if zr is None:
                continue
            if spec.key == "RL2" and rl1_z is not None:   # keep RL2 clearly below RL1
                zr = (zr[0], min(zr[1], rl1_z - int(math.ceil(settings.min_rl_separation_mm / sz))))
            found = _search(ct_c, seg_c, spacing, kernel, radius, labels, spec.side, zr,
                            edge_m, lig_m, out_frac, settings, glob, use_global, use_out)
            if found:
                used_level = lvl
                break
        if not found:
            results.append(RoiResult(spec.key, spec.name, False,
                                     "no valid position found (segment missing or too small)"))
            continue
        res = _measure(ct_c, seg_c, spacing, aff_c, kernel, radius, spec, found, used_level, settings)
        # report voxel indices in the full (uncropped) canonical array
        res.center_voxel = tuple(int(v + o) for v, o in zip(res.center_voxel, offset))
        res.slice_index = res.center_voxel[2]
        if spec.key == "RL1":
            rl1_z = found["z"]
        results.append(res)
    stats = {"liver_typical_hu": None if not np.isfinite(g_mode) else round(g_mode, 1),
             "liver_typical_sd": None if not np.isfinite(g_rsd) else round(g_rsd, 1),
             "outlier_threshold_hu": round(max(settings.outlier_min_hu, 3.0 * (g_rsd if np.isfinite(g_rsd) else 10.0)), 1)}
    return results, stats


def place_spleen_roi(ct, spleen_mask, spacing, affine, settings: Settings) -> tuple[RoiResult, dict]:
    """One circle in the spleen: fully inside it, away from its edge, on the most uniform
    tissue near the spleen's typical HU (avoids vessels at the hilum, cysts, artefacts)."""
    spec = SPLEEN_SPEC
    if spleen_mask is None or not np.any(spleen_mask):
        return RoiResult(spec.key, spec.name, False, "spleen not found in scan"), {}
    spacing = tuple(float(s) for s in spacing)
    sx, sy, _ = spacing
    radius = settings.roi_diameter_mm / 2.0
    kernel = disk_kernel(radius, sx, sy)
    reach = radius + settings.edge_margin_mm + 5.0
    crop = _bbox(spleen_mask, (int(math.ceil(reach / sx)) + 2, int(math.ceil(reach / sy)) + 2, 1))
    ct_c = np.asarray(ct[crop], dtype=np.float32)
    seg_c = np.asarray(spleen_mask[crop], dtype=np.uint8)
    offset = np.array([c.start for c in crop])
    aff_c = affine.copy()
    aff_c[:3, 3] = affine[:3, :3] @ offset + affine[:3, 3]
    g_mode, g_rsd, smooth3 = liver_typical_hu(ct_c, seg_c, spacing)   # same method, on the spleen
    glob = (g_mode, g_rsd, smooth3)
    E, m = settings.edge_margin_mm, settings.max_outlier_fraction
    levels = [(E, 0.0, m, True, True), (E / 2, 0.0, m, True, True), (E / 2, 1.0, 0.15, True, True),
              (E / 2, 0.0, 1.0, False, False)]
    for lvl, (edge_m, widen, out_frac, use_global, use_out) in enumerate(levels):
        f0, f1 = settings.spleen_window
        frac = (max(0.0, f0 - widen), min(1.0, f1 + widen)) if widen < 1.0 else (0.05, 0.95)
        zr = _z_window(seg_c, spec.labels, frac)
        if zr is None:
            break
        found = _search(ct_c, seg_c, spacing, kernel, radius, spec.labels, spec.side, zr,
                        edge_m, 0.0, out_frac, settings, glob, use_global, use_out)
        if found:
            res = _measure(ct_c, seg_c, spacing, aff_c, kernel, radius, spec, found, lvl, settings)
            res.center_voxel = tuple(int(v + o) for v, o in zip(res.center_voxel, offset))
            res.slice_index = res.center_voxel[2]
            stats = {"spleen_typical_hu": None if not np.isfinite(g_mode) else round(g_mode, 1),
                     "spleen_outlier_threshold_hu": round(max(settings.outlier_min_hu, 3.0 * (g_rsd if np.isfinite(g_rsd) else 10.0)), 1)}
            return res, stats
    return RoiResult(spec.key, spec.name, False, "spleen too small for the circle"), {}

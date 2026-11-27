"""Manual adjustment of the ROI circles after automatic placement.

After a case is measured, a compressed copy of the liver + spleen region of the scan
(CT values and the outline labels) is kept next to the result as edit_data.npz, so a
person can later move/resize a circle in the website. Statistics for a moved circle are
computed exactly like the automatic ones (same circle pixels, same SD formula).
The automatic result is kept (roi["auto"]) so a circle can be reset.
"""
from __future__ import annotations

import copy
import datetime as dt
import io
import math
from pathlib import Path

import numpy as np
from scipy import ndimage

from .config import Settings
from .roi import (LEFT_OF_LIGAMENT, ROI_SPECS, SPLEEN_SPEC, RoiResult, _measure, disk_kernel,
                  ligament_points)

EDIT_FILE = "edit_data.npz"
SPLEEN_LABEL = 9          # in the stored label volume: 1-8 = liver segments, 9 = spleen
SPECS = {s.key: s for s in ROI_SPECS + (SPLEEN_SPEC,)}
WINDOW = (-160, 240)


# ---------------------------------------------------------------- saving / loading
def save_edit_data(out_dir: Path, vol, seg, spleen_mask, affine, spacing, slice_positions=None,
                   margin_mm: float = 25.0) -> Path | None:
    """Keep a compressed crop (liver + spleen + margin) of the scan for later editing."""
    labels = seg.astype(np.uint8, copy=True)
    if spleen_mask is not None:
        labels[np.asarray(spleen_mask, bool) & (labels == 0)] = SPLEEN_LABEL
    organs = labels > 0
    if not organs.any():
        return None
    idx = np.nonzero(organs)
    box = []
    for ax in range(3):
        m = int(math.ceil(margin_mm / float(spacing[ax]))) if ax < 2 else 2
        box.append(slice(max(0, int(idx[ax].min()) - m), min(vol.shape[ax], int(idx[ax].max()) + m + 1)))
    box = tuple(box)
    ct = np.asarray(vol[box])
    ct = ct.astype(np.int16) if np.issubdtype(ct.dtype, np.integer) else ct.astype(np.float32)
    offset = np.array([b.start for b in box])
    aff = np.array(affine, float).copy()
    aff[:3, 3] = aff[:3, :3] @ offset + aff[:3, 3]
    path = Path(out_dir) / EDIT_FILE
    np.savez_compressed(path, ct=ct, labels=labels[box], affine=aff, offset=offset,
                        spacing=np.array(spacing, float),
                        slices=np.array([[p[0], -1 if p[1] is None else p[1]] for p in (slice_positions or [])], float).reshape(-1, 2))
    return path


class EditData:
    def __init__(self, case_dir: Path):
        with np.load(Path(case_dir) / EDIT_FILE) as z:
            self.ct = z["ct"]
            self.labels = z["labels"]
            self.affine = z["affine"]
            self.offset = z["offset"].astype(int)
            self.spacing = tuple(float(s) for s in z["spacing"])
            self.slices = z["slices"]
        self.seg = np.where(self.labels <= 8, self.labels, 0).astype(np.uint8)
        self.spleen = (self.labels == SPLEEN_LABEL).astype(np.uint8)

    @staticmethod
    def available(case_dir: Path) -> bool:
        return (Path(case_dir) / EDIT_FILE).exists()

    def to_crop(self, center_voxel) -> tuple[int, int, int]:
        return tuple(int(c) - int(o) for c, o in zip(center_voxel, self.offset))

    def dicom_instance(self, z_mm: float):
        if len(self.slices) == 0:
            return None
        i = int(np.argmin(np.abs(self.slices[:, 0] - z_mm)))
        if abs(self.slices[i, 0] - z_mm) < 0.6 * self.spacing[2] and self.slices[i, 1] >= 0:
            return int(self.slices[i, 1])
        return None


# ---------------------------------------------------------------- measuring
def measure_at(ed: EditData, key: str, x: int, y: int, z: int, diameter_mm: float) -> RoiResult:
    """Measure a circle at crop voxel (x, y, z); same method as the automatic placement."""
    spec = SPECS[key]
    sx, sy, _ = ed.spacing
    diameter_mm = float(min(max(diameter_mm, 3.0), 60.0))
    radius = diameter_mm / 2.0
    kernel = disk_kernel(radius, sx, sy)
    kx, ky = kernel.shape[0] // 2, kernel.shape[1] // 2
    X, Y, Z = ed.ct.shape
    x = int(min(max(x, kx), X - 1 - kx))
    y = int(min(max(y, ky), Y - 1 - ky))
    z = int(min(max(z, 0), Z - 1))
    seg = ed.spleen if key == "SPL" else ed.seg
    found = {"x": x, "y": y, "z": z, "ref": float("nan")}
    res = _measure(ed.ct, seg, ed.spacing, ed.affine, kernel, radius, spec, found, 0, Settings())
    res.center_voxel = tuple(int(v + o) for v, o in zip(res.center_voxel, ed.offset))
    res.slice_index = res.center_voxel[2]
    res.dicom_instance = ed.dicom_instance(res.z_mm)
    return res


def check_circle(ed: EditData, key: str, x: int, y: int, z: int, diameter_mm: float) -> dict:
    """Warnings for a circle placed by hand (not blocking)."""
    sx, sy, _ = ed.spacing
    kernel = disk_kernel(float(diameter_mm) / 2.0, sx, sy)
    kx, ky = kernel.shape[0] // 2, kernel.shape[1] // 2
    organ = (ed.spleen if key == "SPL" else (ed.seg > 0))[x - kx:x + kx + 1, y - ky:y + ky + 1, z]
    inside = float(organ[kernel].mean()) if organ.shape == kernel.shape else 0.0
    warn = []
    if inside < 0.999:
        warn.append(f"{(1 - inside) * 100:.0f}% of the circle is outside the {'spleen' if key == 'SPL' else 'liver'}")
    if key != "SPL":
        lab = int(ed.seg[x, y, z])
        spec = SPECS[key]
        if lab and lab not in spec.fallback_labels and not (key == "LL" and lab in LEFT_OF_LIGAMENT):
            warn.append(f"centre is in segment {lab}, not in the usual segments for {key}")
        if key == "LL" and lab and lab not in LEFT_OF_LIGAMENT:
            warn.append("centre is not left of the falciform ligament (segments 2/3)")
        if key != "LL" and lab in LEFT_OF_LIGAMENT:
            warn.append("centre is left of the falciform ligament")
    return {"inside_fraction": round(inside, 3), "warnings": warn}


# ---------------------------------------------------------------- updating a result
STAT_FIELDS = ("mean_hu", "sd_hu", "min_hu", "max_hu", "median_hu", "area_cm2", "diameter_mm")


def recompute_summary(result: dict) -> None:
    rois = {r["key"]: r for r in result.get("rois", [])}
    liver = [rois[k]["mean_hu"] for k in ("LL", "RL1", "RL2") if k in rois and rois[k].get("placed")]
    right = [rois[k]["mean_hu"] for k in ("RL1", "RL2") if k in rois and rois[k].get("placed")]
    spl = rois.get("SPL")
    lm = float(np.mean(liver)) if liver else None
    sm = spl["mean_hu"] if spl and spl.get("placed") else None
    result["liver_mean_hu_3roi"] = lm
    result["right_lobe_mean_hu"] = float(np.mean(right)) if right else None
    result["spleen_mean_hu"] = sm
    result["liver_minus_spleen_hu"] = (lm - sm) if lm is not None and sm is not None else None
    result["liver_spleen_ratio"] = (lm / sm) if lm is not None and sm is not None and sm > 1.0 else None
    if len(liver) == 3:
        result["status"] = "ok"
    elif liver:
        result["status"] = "partial"


def apply_manual(result: dict, new_roi: RoiResult, user: str) -> dict:
    """Replace one ROI in a result dict by a hand-placed one; keep the automatic one."""
    result = copy.deepcopy(result)
    rois = result.setdefault("rois", [])
    key = new_roi.key
    i = next((j for j, r in enumerate(rois) if r.get("key") == key), None)
    old = rois[i] if i is not None else None
    auto = (old or {}).get("auto") or ({k: v for k, v in old.items() if k not in ("auto", "auto_flags")} if old else None)
    flags = result.get("flags", [])
    auto_flags = (old or {}).get("auto_flags")
    if auto_flags is None:
        auto_flags = [f for f in flags if f.startswith(f"{key} ") or (key == "SPL" and f.startswith("Spleen not"))]
    flags = [f for f in flags if f not in auto_flags and not f.startswith(f"{key} moved by hand")]
    now = dt.datetime.now().isoformat(timespec="seconds")
    new_roi.manual, new_roi.edited_by, new_roi.edited_at = True, user, now
    d = new_roi.as_dict()
    d["auto"], d["auto_flags"] = auto, auto_flags
    if i is None:
        rois.append(d)
    else:
        rois[i] = d
    flags.append(f"{key} moved by hand by {user} ({now.replace('T', ' ')})")
    result["flags"] = flags
    result["revision"] = int(result.get("revision", 0)) + 1
    recompute_summary(result)
    return result


def reset_to_auto(result: dict, key: str) -> dict:
    result = copy.deepcopy(result)
    rois = result.get("rois", [])
    i = next((j for j, r in enumerate(rois) if r.get("key") == key), None)
    if i is None or not rois[i].get("manual"):
        return result
    auto, auto_flags = rois[i].get("auto"), rois[i].get("auto_flags") or []
    if auto is None:
        rois.pop(i)
    else:
        rois[i] = auto
    result["flags"] = [f for f in result.get("flags", []) if not f.startswith(f"{key} moved by hand")] + list(auto_flags)
    result["revision"] = int(result.get("revision", 0)) + 1
    recompute_summary(result)
    return result


def redraw_qa(case_dir: Path, result: dict, title: str) -> None:
    """Re-render qa.png from the kept crop and the (possibly edited) ROIs."""
    from .qa import render_qa
    ed = EditData(case_dir)
    rois, spl = [], None
    for r in result.get("rois", []):
        rr = RoiResult.from_dict(r)
        if rr.placed and rr.center_voxel:
            rr.center_voxel = ed.to_crop(rr.center_voxel)
        if rr.key == "SPL":
            spl = rr
        elif rr.key in ("LL", "RL1", "RL2"):
            rois.append(rr)
    order = {"LL": 0, "RL1": 1, "RL2": 2}
    rois.sort(key=lambda r: order[r.key])
    render_qa(ed.ct, ed.seg, rois, ed.spacing, Path(case_dir) / "qa.png", title=title,
              spleen_mask=ed.spleen.astype(bool) if ed.spleen.any() else None, spleen_roi=spl, summary=result)


# ---------------------------------------------------------------- slice image for the editor
_COLORS = {(2, 3): (0, 229, 255), (4,): (156, 255, 87), (5, 6, 7, 8): (255, 77, 210), (1,): (176, 176, 176),
           (SPLEEN_LABEL,): (74, 222, 128)}


def slice_png(ed: EditData, z: int) -> bytes:
    """Axial slice (radiological view: patient right on the left, anterior up) with outlines."""
    from PIL import Image
    z = int(min(max(z, 0), ed.ct.shape[2] - 1))
    sl = ed.ct[:, :, z].astype(np.float32)
    g = np.clip((sl - WINDOW[0]) / (WINDOW[1] - WINDOW[0]) * 255.0, 0, 255).astype(np.uint8)
    rgb = np.repeat(g[..., None], 3, axis=2)
    lab = ed.labels[:, :, z]
    for labels, color in _COLORS.items():
        m = np.isin(lab, labels)
        if m.any():
            edge = m & ~ndimage.binary_erosion(m)
            rgb[edge] = color
    pts = ligament_points(ed.seg[:, :, z])
    for x, y in pts:
        rgb[x, y] = (255, 255, 0)
    disp = rgb[::-1, ::-1].transpose(1, 0, 2)          # canonical (x, y) -> display rows/cols
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(disp)).save(buf, format="PNG", optimize=False)
    return buf.getvalue()

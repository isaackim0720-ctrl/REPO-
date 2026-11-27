"""Automatic checks: mean HU too high / too low, incomplete scan, poor image quality.

Contrast phase matters for what counts as a normal HU, so it is estimated first:
  * aorta HU from the coarse model (blood ~30-60 HU without contrast, >130 HU with),
  * else the DICOM ContrastBolusAgent tag, else the spleen HU.
All ranges and limits are Settings so a lab can match its own protocol.
"""
from __future__ import annotations

import numpy as np

from .config import Settings

PHASE_NON = "non-contrast"
PHASE_CON = "contrast-enhanced"
PHASE_UNK = "uncertain"


def contrast_phase(aorta_hu, dicom_tag: str, spleen_mean, settings: Settings) -> tuple[str, str]:
    """Return (phase, how it was decided)."""
    if aorta_hu is not None and np.isfinite(aorta_hu):
        if aorta_hu >= settings.aorta_contrast_hu:
            return PHASE_CON, f"aorta {aorta_hu:.0f} HU"
        if aorta_hu < settings.aorta_noncontrast_hu:
            if dicom_tag:
                return PHASE_UNK, f"aorta {aorta_hu:.0f} HU (looks non-contrast) but DICOM says contrast '{dicom_tag}'"
            return PHASE_NON, f"aorta {aorta_hu:.0f} HU"
        return PHASE_UNK, f"aorta {aorta_hu:.0f} HU (between non-contrast and contrast)"
    if dicom_tag:
        return PHASE_CON, f"DICOM contrast tag '{dicom_tag}'"
    if spleen_mean is not None and np.isfinite(spleen_mean):
        if spleen_mean > 80:
            return PHASE_CON, f"spleen {spleen_mean:.0f} HU"
        if spleen_mean < 70:
            return PHASE_NON, f"spleen {spleen_mean:.0f} HU"
    return PHASE_UNK, "aorta not found and no DICOM contrast information"


def hu_ranges(phase: str, settings: Settings) -> dict:
    """Expected mean-HU range per circle for this phase (uncertain: widest of both)."""
    nl, ns = tuple(settings.hu_range_liver_noncontrast), tuple(settings.hu_range_spleen_noncontrast)
    cl, cs = tuple(settings.hu_range_liver_contrast), tuple(settings.hu_range_spleen_contrast)
    if phase == PHASE_NON:
        liver, spleen = nl, ns
    elif phase == PHASE_CON:
        liver, spleen = cl, cs
    else:
        liver, spleen = (min(nl[0], cl[0]), max(nl[1], cl[1])), (min(ns[0], cs[0]), max(ns[1], cs[1]))
    out = {k: [float(liver[0]), float(liver[1])] for k in ("LL", "RL1", "RL2")}
    out["SPL"] = [float(spleen[0]), float(spleen[1])]
    return out


def check_hu(mean, rng) -> str:
    if mean is None or rng is None or not np.isfinite(mean):
        return ""
    if mean < rng[0]:
        return "low"
    if mean > rng[1]:
        return "high"
    return ""


def apply_hu_checks(result: dict) -> None:
    """Set roi['hu_check'] ('low' / 'high' / '') from result['hu_ranges'] and refresh the
    HU flags. Used after measuring and again after a circle is moved by hand."""
    ranges = result.get("hu_ranges") or {}
    phase = result.get("contrast_phase") or ""
    flags = [f for f in result.get("flags", []) if " mean HU is " not in f]
    for r in result.get("rois", []):
        rng = ranges.get(r.get("key"))
        c = check_hu(r.get("mean_hu"), rng) if r.get("placed") else ""
        r["hu_check"] = c
        if c:
            organ = "spleen" if r["key"] == "SPL" else "liver"
            bound = f"< {rng[0]:.0f}" if c == "low" else f"> {rng[1]:.0f}"
            flags.append(f"{r['key']} mean HU is {c.upper()} ({r['mean_hu']:.0f} HU; expected {rng[0]:.0f}-{rng[1]:.0f} HU "
                         f"for {phase or 'this'} {organ}, {bound})")
    result["flags"] = flags


def _touches_inplane(mask: np.ndarray, min_voxels: int = 20) -> bool:
    border = (mask[0].sum() + mask[-1].sum() + mask[:, 0].sum() + mask[:, -1].sum())
    return int(border) >= min_voxels


def completeness(seg: np.ndarray, spleen_mask, slice_positions=None) -> tuple[bool, list[str]]:
    """Is the whole liver in the scan? Returns (complete, notes). Spleen problems are
    noted but do not make the scan 'incomplete' (the liver circles are still valid)."""
    notes: list[str] = []
    complete = True
    liver = seg > 0
    zs = np.nonzero(liver.any(axis=(0, 1)))[0]
    if len(zs):
        if zs.min() == 0:
            notes.append("liver is cut off at the bottom of the scan")
            complete = False
        if zs.max() == seg.shape[2] - 1:
            notes.append("liver is cut off at the top of the scan")
            complete = False
    if _touches_inplane(liver):
        notes.append("liver is cut off at the side of the image (field of view too small)")
        complete = False
    if slice_positions:
        z = np.unique(np.round(np.array([p[0] for p in slice_positions], float), 3))
        if len(z) > 3:
            d = np.diff(z)
            med = float(np.median(d))
            if med > 0:
                missing = int(np.sum(np.maximum(np.round(d / med) - 1, 0)))
                if missing > 0:
                    notes.append(f"{missing} slice(s) missing from the series (gap in slice positions)")
                    complete = False
    if spleen_mask is not None and np.any(spleen_mask):
        sz = np.nonzero(np.asarray(spleen_mask).any(axis=(0, 1)))[0]
        if sz.min() == 0 or sz.max() == seg.shape[2] - 1 or _touches_inplane(np.asarray(spleen_mask, bool)):
            notes.append("spleen is partly outside the scan")
    return complete, notes


def quality(rois, meta: dict, liver_stats: dict, liver_ml: float, settings: Settings,
            single_slice_ok=True) -> tuple[str, list[str], float | None]:
    """Return (level 'good' / 'check' / 'poor', notes, noise in HU)."""
    poor, check = [], []
    placed = [r for r in rois if r.placed]
    noise = float(np.median([r.sd_hu for r in placed])) if placed else None
    if len(placed) < len(rois):
        poor.append("not all liver circles could be placed")
    if meta.get("warning_not_hu"):
        poor.append("image values are not Hounsfield units")
    if noise is not None:
        if noise > settings.noise_poor_hu:
            poor.append(f"image very noisy (SD {noise:.0f} HU in the circles)")
        elif noise > settings.noise_check_hu:
            check.append(f"noisy image (SD {noise:.0f} HU in the circles)")
    st = meta.get("slice_thickness_mm")
    if st and st > 5.0:
        check.append(f"thick slices ({st} mm)")
    if liver_ml < 500 or liver_ml > 4500:
        check.append(f"unusual liver volume ({liver_ml:.0f} mL) - check the liver outline")
    frac = liver_stats.get("liver_outlier_fraction")
    if frac is not None and frac > settings.heterogeneity_check_fraction:
        check.append(f"liver very uneven ({frac * 100:.0f}% unusual tissue: lesions, artefacts or patchy fat)")
    if any(r.fallback_level >= 3 for r in placed):
        check.append("a circle needed relaxed placement rules")
    if not single_slice_ok:
        check.append("liver circles could not all go on one slice")
    level = "poor" if poor else ("check" if check else "good")
    return level, poor + check, noise

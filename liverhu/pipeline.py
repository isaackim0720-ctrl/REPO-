"""One case end-to-end: load scan -> segment liver -> place 3 ROIs -> measure -> QA image."""
from __future__ import annotations

import datetime as dt
import json
import time
import traceback
from pathlib import Path

import numpy as np

from . import __version__
from .config import Settings
from .roi import place_rois_with_stats, place_spleen_roi, ROI_SPECS, SPLEEN_SPEC
from .scanio import CaseInput, load_case

ROI_KEYS = [s.key for s in ROI_SPECS] + [SPLEEN_SPEC.key]


def _versions() -> dict:
    from importlib.metadata import version, PackageNotFoundError
    out = {"liverhu": __version__}
    for pkg in ("TotalSegmentator", "nnunetv2", "torch"):
        try:
            out[pkg] = version(pkg)
        except PackageNotFoundError:
            out[pkg] = None
    return out


def _flags(rois, meta, liver_touch, liver_ml, liver_stats=None, spleen=None, spleen_stats=None) -> list[str]:
    flags = []
    typ = (liver_stats or {}).get("liver_typical_hu")
    thr = (liver_stats or {}).get("outlier_threshold_hu") or 20.0
    if spleen is not None:
        if not spleen.placed:
            flags.append(f"Spleen not measured: {spleen.reason}")
        else:
            if spleen.fallback_level > 0:
                flags.append(f"SPL placed with relaxed rules (level {spleen.fallback_level}) - check image")
            if spleen.sd_hu is not None and spleen.sd_hu > 25:
                flags.append(f"SPL SD {spleen.sd_hu:.0f} HU is high (noisy scan or vessel/lesion) - check image")
            st = (spleen_stats or {}).get("spleen_typical_hu")
            sthr = (spleen_stats or {}).get("spleen_outlier_threshold_hu") or 20.0
            if st is not None and abs(spleen.mean_hu - st) > sthr:
                flags.append(f"SPL mean {spleen.mean_hu:.0f} HU differs from the typical spleen value {st:.0f} HU - check image")
    for r in rois:
        if not r.placed:
            flags.append(f"{r.key} not placed: {r.reason}")
            continue
        if typ is not None and abs(r.mean_hu - typ) > thr:
            flags.append(f"{r.key} mean {r.mean_hu:.0f} HU differs from the typical liver value {typ:.0f} HU "
                         "(lesion or focal fat?) - check image")
        if not r.side_ok:
            flags.append(f"{r.key} may be on the wrong side of the falciform ligament - check image")
        if r.fallback_level > 0:
            flags.append(f"{r.key} placed with relaxed rules (level {r.fallback_level}) - check image")
        if r.sd_hu is not None and r.sd_hu > 25:
            flags.append(f"{r.key} SD {r.sd_hu:.0f} HU is high (noisy scan or vessel/lesion) - check image")
    if meta.get("contrast_agent_tag"):
        flags.append("Contrast agent recorded in DICOM: fatty-liver HU cut-offs are for non-contrast CT")
    st = meta.get("slice_thickness_mm")
    if st and st > 5.0:
        flags.append(f"Slice thickness {st} mm > 5 mm")
    if liver_touch:
        flags.append("Liver touches the top/bottom of the scan (liver not fully covered)")
    if liver_ml < 500 or liver_ml > 4500:
        flags.append(f"Unusual liver volume {liver_ml:.0f} mL - check segmentation")
    if meta.get("warning_not_hu"):
        flags.append(meta["warning_not_hu"])
    return flags


def process_case(case: CaseInput, out_dir: Path, settings: Settings, log=print) -> dict:
    """Run one case and write result.json + qa.png into out_dir. Never raises."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    result: dict = {"case_id": case.case_id, "status": "failed", "error": None,
                    "processed_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "settings": settings.to_dict(), "software": _versions()}
    try:
        from .segment import segment_liver_and_spleen
        from .qa import render_qa
        log(f"[{case.case_id}] loading scan")
        img, meta = load_case(case)
        vol = np.asanyarray(img.dataobj)
        spacing = [float(z) for z in img.header.get_zooms()[:3]]
        slice_positions = meta.pop("slice_positions", None)
        result["input"] = {k: v for k, v in meta.items()}
        result["input"]["other_series"] = case.other_series
        log(f"[{case.case_id}] step 1 of 4: finding the liver and spleen ({vol.shape[2]} slices)")
        seg, spleen_mask, seg_info = segment_liver_and_spleen(
            vol, spacing, log=lambda m: log(f"[{case.case_id}] {m}"), spleen=settings.measure_spleen)
        voxel_ml = float(np.prod(spacing)) / 1000.0
        counts = np.bincount(seg.ravel(), minlength=9)
        liver_ml = float(counts[1:].sum() * voxel_ml)
        zs = np.nonzero((seg > 0).any(axis=(0, 1)))[0]
        liver_touch = bool(len(zs) and (zs.min() == 0 or zs.max() == seg.shape[2] - 1))
        log(f"[{case.case_id}] step 4 of 4: placing the circles and measuring")
        rois, liver_stats = place_rois_with_stats(vol, seg, spacing, img.affine, settings)
        spleen, spleen_stats = None, {}
        if settings.measure_spleen:
            spleen, spleen_stats = place_spleen_roi(vol, spleen_mask, spacing, img.affine, settings)
        if slice_positions:
            zpos = np.array([p[0] for p in slice_positions])
            for r in rois + ([spleen] if spleen is not None else []):
                if r.placed:
                    i = int(np.argmin(np.abs(zpos - r.z_mm)))
                    if abs(zpos[i] - r.z_mm) < 0.6 * spacing[2]:
                        r.dicom_instance = slice_positions[i][1]
        placed = [r for r in rois if r.placed]
        liver_mean = float(np.mean([r.mean_hu for r in placed])) if placed else None
        spleen_mean = spleen.mean_hu if spleen is not None and spleen.placed else None
        result.update(
            status="ok" if len(placed) == len(rois) else ("partial" if placed else "failed"),
            rois=[r.as_dict() for r in rois] + ([spleen.as_dict()] if spleen is not None else []),
            liver_mean_hu_3roi=liver_mean,
            spleen_mean_hu=spleen_mean,
            spleen_typical_hu=spleen_stats.get("spleen_typical_hu"),
            spleen_volume_ml=(round(float(spleen_mask.sum()) * float(np.prod(spacing)) / 1000.0, 1)
                              if spleen_mask is not None else None),
            # liver attenuation index (L - S) and liver-to-spleen ratio (L / S), using the 3-ROI liver mean
            liver_minus_spleen_hu=(liver_mean - spleen_mean) if liver_mean is not None and spleen_mean is not None else None,
            liver_spleen_ratio=(liver_mean / spleen_mean) if liver_mean is not None and spleen_mean is not None and spleen_mean > 1.0 else None,
            right_lobe_mean_hu=(float(np.mean([r.mean_hu for r in placed if r.key != "LL"]))
                                if any(r.key != "LL" for r in placed) else None),
            liver_typical_hu=liver_stats.get("liver_typical_hu"),
            liver_typical_sd=liver_stats.get("liver_typical_sd"),
            liver_volume_ml=round(liver_ml, 1),
            segment_volumes_ml={str(k): round(float(counts[k] * voxel_ml), 1) for k in range(1, 9)},
            liver_fully_covered=not liver_touch,
            segmentation=seg_info,
        )
        result["flags"] = _flags(rois, result["input"], liver_touch, liver_ml, liver_stats, spleen, spleen_stats)
        if not placed:
            result["error"] = "No ROI could be placed"
        log(f"[{case.case_id}] step 4 of 4: drawing the check image")
        title = case.case_id
        if placed:
            title += f"   liver mean (3 ROIs) {liver_mean:.1f} HU"
        if spleen_mean is not None:
            title += f"   spleen {spleen_mean:.1f} HU"
        render_qa(vol, seg, rois, spacing, out_dir / "qa.png", title=title,
                  spleen_mask=spleen_mask, spleen_roi=spleen, summary=result)
        if settings.keep_edit_data:
            from .edit import save_edit_data
            if save_edit_data(out_dir, vol, seg, spleen_mask, img.affine, spacing, slice_positions):
                result["editable"] = True
        if settings.save_segmentation:
            import nibabel as nib
            nib.save(nib.Nifti1Image(seg, img.affine), out_dir / "liver_segments.nii.gz")
            if spleen_mask is not None:
                nib.save(nib.Nifti1Image(spleen_mask.astype(np.uint8), img.affine), out_dir / "spleen.nii.gz")
    except Exception as e:  # report, don't crash the batch/server
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc(limit=8)
        result.setdefault("flags", [])
        log(f"[{case.case_id}] FAILED: {result['error']}")
    result["seconds"] = round(time.time() - t0, 1)
    (out_dir / "result.json").write_text(json.dumps(result, indent=2, default=_json_default))
    return result


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def flatten(result: dict) -> dict:
    """One spreadsheet row per case."""
    row = {"case_id": result.get("case_id"), "status": result.get("status")}
    rois = {r["key"]: r for r in result.get("rois", [])}
    for key in ROI_KEYS:
        r = rois.get(key, {})
        row.update({
            f"{key}_mean_HU": _r(r.get("mean_hu")), f"{key}_SD_HU": _r(r.get("sd_hu")),
            f"{key}_min_HU": _r(r.get("min_hu"), 0), f"{key}_max_HU": _r(r.get("max_hu"), 0),
            f"{key}_area_cm2": _r(r.get("area_cm2"), 2), f"{key}_diameter_mm": _r(r.get("diameter_mm")),
            f"{key}_z_mm": _r(r.get("z_mm")), f"{key}_image_no": r.get("dicom_instance"),
            f"{key}_segment": r.get("segment_at_center"),
            f"{key}_side_of_ligament": r.get("side_of_ligament"),
            f"{key}_manual": ("yes, by " + (r.get("edited_by") or "?")) if r.get("manual") else "",
        })
    inp = result.get("input", {})
    row.update({
        "liver_mean_HU_3ROI": _r(result.get("liver_mean_hu_3roi")),
        "right_lobe_mean_HU": _r(result.get("right_lobe_mean_hu")),
        "liver_typical_HU": _r(result.get("liver_typical_hu")),
        "spleen_mean_HU": _r(result.get("spleen_mean_hu")),
        "liver_minus_spleen_HU": _r(result.get("liver_minus_spleen_hu")),
        "liver_spleen_ratio": _r(result.get("liver_spleen_ratio"), 3),
        "liver_volume_mL": result.get("liver_volume_ml"),
        "spleen_volume_mL": result.get("spleen_volume_ml"),
        "series_description": inp.get("series_description"),
        "slice_thickness_mm": inp.get("slice_thickness_mm"),
        "kVp": inp.get("kvp"),
        "contrast_agent_tag": inp.get("contrast_agent_tag"),
        "study_key": inp.get("study_key"),
    })
    for k in ("patient_id", "accession_number", "study_date"):
        if k in inp:
            row[k] = inp[k]
    sw = result.get("software") or {}
    row.update({"flags": " | ".join(result.get("flags", [])), "error": result.get("error"),
                "processed_at": result.get("processed_at"),
                "roi_diameter_setting_mm": (result.get("settings") or {}).get("roi_diameter_mm"),
                "model_tile": (result.get("segmentation") or {}).get("tile"),
                "software": f"liverhu {sw.get('liverhu') or '?'} / TotalSegmentator {sw.get('TotalSegmentator') or '?'}"})
    return row


def _r(v, nd=1):
    return None if v is None else round(float(v), nd)


def write_tables(results: list[dict], out_dir: Path) -> tuple[Path, Path]:
    import pandas as pd
    out_dir = Path(out_dir)
    df = pd.DataFrame([flatten(r) for r in results])
    csv_path, xlsx_path = out_dir / "liver_hu_results.csv", out_dir / "liver_hu_results.xlsx"
    df.to_csv(csv_path, index=False)
    df.to_excel(xlsx_path, index=False)
    return csv_path, xlsx_path

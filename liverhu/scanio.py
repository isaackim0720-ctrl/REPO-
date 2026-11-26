"""Find and load CT scans: DICOM folders (any layout, many studies), .zip files, NIfTI.

Privacy: patient name is never read. PatientID / AccessionNumber / StudyDate are only
returned when include_identifiers=True. Each study gets a pseudonymous key (hash of its
StudyInstanceUID) so results can still be linked back by whoever holds the UIDs.
"""
from __future__ import annotations

import hashlib
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import nibabel as nib
import numpy as np

NIFTI_SUFFIXES = (".nii", ".nii.gz")
_TAGS = ["Modality", "StudyInstanceUID", "SeriesInstanceUID", "SeriesDescription", "SeriesNumber",
         "SliceThickness", "ImageOrientationPatient", "ImagePositionPatient", "ImageType", "KVP",
         "ContrastBolusAgent", "Rows", "Columns", "InstanceNumber", "PixelSpacing",
         "PatientID", "AccessionNumber", "StudyDate", "SOPClassUID"]


@dataclass
class CaseInput:
    case_id: str
    kind: str                           # "nifti" or "dicom"
    path: str = ""                      # NIfTI file
    files: list = field(default_factory=list)   # DICOM files of the chosen series (sorted)
    meta: dict = field(default_factory=dict)
    other_series: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"case_id": self.case_id, "kind": self.kind, "path": self.path,
                "files": self.files, "meta": self.meta, "other_series": self.other_series}

    @classmethod
    def from_dict(cls, d: dict) -> "CaseInput":
        return cls(**d)


def safe_name(text: str, fallback: str = "case") -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip("._")
    return text[:80] or fallback


def uid_hash(uid: str) -> str:
    return hashlib.sha256(uid.encode()).hexdigest()[:12] if uid else ""


def is_nifti(p: Path) -> bool:
    n = p.name.lower()
    return n.endswith(".nii") or n.endswith(".nii.gz")


def safe_extract_zip(zpath: Path, dest: Path, max_total_bytes: int = 40 * 1024 ** 3) -> None:
    """Extract a zip, refusing absolute paths, '..' traversal and absurd total sizes."""
    dest = dest.resolve()
    with zipfile.ZipFile(zpath) as zf:
        total = sum(i.file_size for i in zf.infolist())
        if total > max_total_bytes:
            raise ValueError(f"{zpath.name}: unpacked size {total / 1e9:.1f} GB is too large")
        for info in zf.infolist():
            if info.is_dir():
                continue
            target = (dest / info.filename).resolve()
            if dest not in target.parents:
                raise ValueError(f"{zpath.name}: unsafe path inside zip: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                while chunk := src.read(1 << 20):
                    out.write(chunk)


def expand_zips(root: Path) -> None:
    """Extract every .zip under root (in place, next to the zip) and delete the zip."""
    for z in sorted(root.rglob("*.zip")):
        out = z.with_name(safe_name(z.stem, "zip") + "_unzipped")
        out.mkdir(exist_ok=True)
        safe_extract_zip(z, out)
        z.unlink()


def _read_header(path: Path):
    import pydicom
    try:
        ds = pydicom.dcmread(str(path), stop_before_pixels=True, specific_tags=_TAGS, force=True)
    except Exception:
        return None
    if not getattr(ds, "SeriesInstanceUID", None) or not hasattr(ds, "Rows"):
        return None
    return ds


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _series_summary(hdrs: list, include_identifiers: bool) -> dict:
    h = hdrs[0]
    iop = [_f(v, 0.0) for v in getattr(h, "ImageOrientationPatient", [1, 0, 0, 0, 1, 0])]
    normal = np.cross(iop[:3], iop[3:6]) if len(iop) == 6 else np.array([0, 0, 1.0])
    image_type = [str(t).upper() for t in getattr(h, "ImageType", [])]
    contrast = str(getattr(h, "ContrastBolusAgent", "") or "").strip()
    meta = {
        "modality": str(getattr(h, "Modality", "")),
        "series_description": str(getattr(h, "SeriesDescription", "") or ""),
        "series_number": str(getattr(h, "SeriesNumber", "") or ""),
        "n_images": len(hdrs),
        "slice_thickness_mm": _f(getattr(h, "SliceThickness", None)),
        "kvp": _f(getattr(h, "KVP", None)),
        "contrast_agent_tag": contrast,
        "axial": bool(abs(normal[2]) >= 0.9),
        "localizer": "LOCALIZER" in image_type,
        "study_key": uid_hash(str(getattr(h, "StudyInstanceUID", ""))),
    }
    if include_identifiers:
        meta.update(patient_id=str(getattr(h, "PatientID", "") or ""),
                    accession_number=str(getattr(h, "AccessionNumber", "") or ""),
                    study_date=str(getattr(h, "StudyDate", "") or ""))
    return meta


def _sort_files(files_hdrs: list) -> list:
    """Sort a series' files along the slice normal (fallback: InstanceNumber)."""
    h0 = files_hdrs[0][1]
    iop = [_f(v, 0.0) for v in getattr(h0, "ImageOrientationPatient", [1, 0, 0, 0, 1, 0])]
    normal = np.cross(iop[:3], iop[3:6]) if len(iop) == 6 else np.array([0, 0, 1.0])

    def key(fh):
        ipp = getattr(fh[1], "ImagePositionPatient", None)
        if ipp is not None and len(ipp) == 3:
            return float(np.dot(normal, [_f(v, 0.0) for v in ipp]))
        return float(_f(getattr(fh[1], "InstanceNumber", 0), 0.0))
    return sorted(files_hdrs, key=key)


def choose_series(series: list[dict], preferred_slice_mm: float) -> int | None:
    """Index of the best series for liver HU measurement, or None."""
    ok = [i for i, s in enumerate(series)
          if s["modality"] == "CT" and s["axial"] and not s["localizer"] and s["n_images"] >= 20]
    if not ok:
        return None

    def rank(i):
        s = series[i]
        t = s["slice_thickness_mm"] or preferred_slice_mm
        return (1 if s["contrast_agent_tag"] else 0,     # prefer non-contrast series
                abs(t - preferred_slice_mm),
                -s["n_images"])
    return min(ok, key=rank)


def discover_cases(root: Path, label: str = "", preferred_slice_mm: float = 3.0,
                   include_identifiers: bool = False) -> tuple[list[CaseInput], list[str]]:
    """Find all cases under root. Returns (cases, warnings)."""
    root = Path(root)
    warnings: list[str] = []
    cases: list[CaseInput] = []
    if root.is_file():
        files = [root]
        base = root.parent
    else:
        expand_zips(root)
        files = [p for p in root.rglob("*") if p.is_file()]
        base = root
    used_ids: set[str] = set()

    def unique(cid: str) -> str:
        cid = safe_name(cid)
        out, n = cid, 2
        while out in used_ids:
            out, n = f"{cid}_{n}", n + 1
        used_ids.add(out)
        return out

    dicom_candidates = []
    for p in sorted(files):
        if is_nifti(p):
            stem = p.name[:-7] if p.name.lower().endswith(".nii.gz") else p.stem
            cases.append(CaseInput(unique(stem if not label else f"{label}_{stem}"), "nifti", path=str(p)))
        elif p.name.startswith(".") or p.suffix.lower() in (".txt", ".pdf", ".jpg", ".png", ".xml", ".json", ".csv", ".exe", ".dll", ".ini", ".htm", ".html"):
            continue
        else:
            dicom_candidates.append(p)

    # group DICOM by study -> series
    studies: dict[str, dict[str, list]] = {}
    for p in dicom_candidates:
        h = _read_header(p)
        if h is None:
            continue
        st = str(getattr(h, "StudyInstanceUID", "unknown"))
        se = str(h.SeriesInstanceUID)
        studies.setdefault(st, {}).setdefault(se, []).append((str(p), h))

    # folder of each study relative to the upload root; when several studies all sit in
    # one top folder (the folder that was uploaded), leave that folder out of the names
    study_rel: dict[str, Path] = {}
    for st_uid, series_map in studies.items():
        common = Path(os.path.commonpath([f for fh in series_map.values() for f, _ in fh]))
        if common.is_file():
            common = common.parent
        study_rel[st_uid] = common.relative_to(base) if common != base and base in common.parents else Path("")
    firsts = {r.parts[0] for r in study_rel.values() if r.parts}
    strip_top = (len(studies) > 1 and len(firsts) == 1
                 and all(len(r.parts) > 1 for r in study_rel.values()))

    for n, (st_uid, series_map) in enumerate(sorted(studies.items(), key=lambda kv: min(f for f, _ in next(iter(kv[1].values())))), 1):
        summaries, file_lists, positions = [], [], []
        for se_uid, fh in series_map.items():
            fh = _sort_files(fh)
            s = _series_summary([h for _, h in fh], include_identifiers)
            summaries.append(s)
            file_lists.append([f for f, _ in fh])
            pos = []
            for _, h in fh:
                ipp = getattr(h, "ImagePositionPatient", None)
                inst = getattr(h, "InstanceNumber", None)
                if ipp is not None and len(ipp) == 3:
                    pos.append([round(float(ipp[2]), 3), int(inst) if inst not in (None, "") else None])
            positions.append(pos)
        rel = study_rel[st_uid]
        if strip_top and len(rel.parts) > 1:
            rel = Path(*rel.parts[1:])
        name_part = "_".join(rel.parts[-2:]) if rel.parts else f"study{n:03d}"
        cid = unique(f"{label}_{name_part}" if label else name_part)
        idx = choose_series(summaries, preferred_slice_mm)
        if idx is None:
            warnings.append(f"{cid}: no axial CT series with >= 20 images found; skipped")
            continue
        others = [{k: s[k] for k in ("series_description", "series_number", "n_images",
                                      "slice_thickness_mm", "contrast_agent_tag")}
                  for i, s in enumerate(summaries) if i != idx]
        meta = dict(summaries[idx], slice_positions=positions[idx])
        cases.append(CaseInput(cid, "dicom", files=file_lists[idx], meta=meta, other_series=others))
    if not cases:
        warnings.append("No CT scans found (expected DICOM files, a .zip of them, or .nii/.nii.gz)")
    return cases, warnings


def _sitk_to_nifti(img) -> nib.Nifti1Image:
    import SimpleITK as sitk
    arr = sitk.GetArrayFromImage(img).transpose(2, 1, 0)        # -> (x, y, z)
    spacing = np.array(img.GetSpacing(), float)
    direction = np.array(img.GetDirection(), float).reshape(3, 3)
    affine = np.eye(4)
    affine[:3, :3] = direction @ np.diag(spacing)
    affine[:3, 3] = np.array(img.GetOrigin(), float)
    affine = np.diag([-1.0, -1.0, 1.0, 1.0]) @ affine           # DICOM LPS -> NIfTI RAS
    if arr.dtype not in (np.int16, np.float32):
        arr = arr.astype(np.float32) if np.issubdtype(arr.dtype, np.floating) or arr.max() > 32767 or arr.min() < -32768 else arr.astype(np.int16)
    return nib.Nifti1Image(arr, affine)


def load_case(case: CaseInput) -> tuple[nib.Nifti1Image, dict]:
    """Load a case as a canonical (RAS+) NIfTI image in Hounsfield units."""
    meta = dict(case.meta)
    if case.kind == "nifti":
        img = nib.load(case.path)
        data = np.asanyarray(img.dataobj)
        if np.issubdtype(data.dtype, np.integer) and img.dataobj.slope in (1, None) and img.dataobj.inter in (0, None):
            data = data.astype(np.int16) if data.min() >= -32768 and data.max() <= 32767 else data.astype(np.float32)
        else:
            data = img.get_fdata(dtype=np.float32)
        img = nib.Nifti1Image(data, img.affine)
        meta.setdefault("series_description", Path(case.path).name)
        meta.setdefault("contrast_agent_tag", "")
    else:
        import SimpleITK as sitk
        reader = sitk.ImageSeriesReader()
        reader.SetFileNames(case.files)
        sitk_img = reader.Execute()
        img = _sitk_to_nifti(sitk_img)
    img = nib.as_closest_canonical(img)
    zooms = [float(z) for z in img.header.get_zooms()[:3]]
    meta.update(shape=list(img.shape[:3]), voxel_size_mm=[round(z, 4) for z in zooms])
    if meta.get("slice_thickness_mm") is None:
        meta["slice_thickness_mm"] = round(zooms[2], 3)
    data = np.asanyarray(img.dataobj)
    if float(np.percentile(data[::4, ::4, ::4], 1)) > -500:
        meta["warning_not_hu"] = "image values do not look like Hounsfield units (no air below -500)"
    return img, meta

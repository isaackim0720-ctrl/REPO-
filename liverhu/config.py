"""Measurement settings. Defaults follow common practice for CT liver-attenuation ROIs;
change them to match your lab's protocol (they are also editable on the website)."""
from __future__ import annotations

from dataclasses import dataclass, asdict, fields


@dataclass
class Settings:
    # Circle size. 15 mm diameter = 1.77 cm2.
    roi_diameter_mm: float = 15.0
    # Minimum gap between the circle edge and the liver/segment boundary (partial-volume guard).
    edge_margin_mm: float = 5.0
    # Minimum gap between the circle edge and the estimated falciform ligament.
    ligament_margin_mm: float = 10.0
    # A circle may contain at most this fraction of "outlier" pixels (vessels, lesions, artefact).
    max_outlier_fraction: float = 0.05
    # Pixels differing from the regional median by more than max(this, 3 x robust SD) are outliers.
    outlier_min_hu: float = 20.0
    # Placement score = local SD + weight * |local mean - regional median|.
    homogeneity_weight: float = 0.5
    # Slice windows as fractions of the structure's top-bottom extent (0 = bottom, 1 = top).
    ll_window: tuple = (0.25, 0.75)          # segments 2/3
    rl_top_window: tuple = (0.60, 0.85)      # right lobe, upper ROI (RL1)
    rl_bottom_window: tuple = (0.15, 0.40)   # right lobe, lower ROI (RL2)
    # Couinaud segments for the right-lobe circles (first choice; RL1 falls back to 7/8,
    # then the whole right lobe, only if the circle does not fit). Segment 8 = anterior
    # upper right lobe, 7 = posterior upper, 5 = anterior lower, 6 = posterior lower.
    rl1_segments: tuple = (8,)
    rl2_segments: tuple = (5, 6)
    # One-slice mode (default): LL, RL1 and RL2 on the SAME axial slice - the slice where all
    # three fit in the most uniform tissue. RL1 = front of the right lobe (top of the image,
    # segments 5/8), RL2 = back (bottom of the image, segments 6/7). The spleen circle uses
    # that slice too when the spleen fits there, otherwise its own best slice.
    single_slice: bool = True
    one_slice_window: tuple = (0.20, 0.80)   # slices considered: middle part of the liver
    one_slice_rl1_segments: tuple = (5, 8)
    one_slice_rl2_segments: tuple = (6, 7)
    spleen_window: tuple = (0.25, 0.75)      # spleen ROI: middle part of the spleen
    # Also place one circle in the spleen (mean/SD/min/max) and report liver-spleen metrics.
    measure_spleen: bool = True
    # RL2 must be at least this far (cranio-caudally) below RL1.
    min_rl_separation_mm: float = 30.0
    # When a DICOM study has several series, prefer slice thickness closest to this.
    preferred_slice_mm: float = 3.0
    # ---- automatic checks ----
    # A circle's MEAN HU outside its range gets a warning sign. Non-contrast defaults:
    # liver < 40 HU = moderate-severe fatty liver cut-off (Boyce et al., AJR 2010);
    # > 80 HU ~ mean + 2 SD of 3,357 adults in that study (58.8 +/- 10.8 HU).
    # Spleen and contrast-enhanced ranges are rough plausibility ranges; adjust to your protocol.
    hu_range_liver_noncontrast: tuple = (40.0, 80.0)
    hu_range_spleen_noncontrast: tuple = (30.0, 65.0)
    hu_range_liver_contrast: tuple = (60.0, 170.0)
    hu_range_spleen_contrast: tuple = (70.0, 180.0)
    # Contrast phase from the aorta: >= this = contrast-enhanced, < the other = non-contrast.
    aorta_contrast_hu: float = 130.0
    aorta_noncontrast_hu: float = 90.0
    # Image quality: median SD of the liver circles above these = "check" / "poor".
    noise_check_hu: float = 25.0
    noise_poor_hu: float = 40.0
    # More than this fraction of unusual liver tissue = "check" (lesions/artefacts/patchy fat).
    heterogeneity_check_fraction: float = 0.30
    # Also save the liver-segment mask (NIfTI) with each result.
    save_segmentation: bool = False
    # Keep a compressed copy of the liver + spleen region so circles can be adjusted by
    # hand later in the website (deleted together with the upload).
    keep_edit_data: bool = True

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, tuple):
                d[k] = list(v)
        return d

    @classmethod
    def from_dict(cls, d: dict | None) -> "Settings":
        s = cls()
        if not d:
            return s
        for f in fields(cls):
            if f.name in d and d[f.name] is not None:
                v = d[f.name]
                cur = getattr(s, f.name)
                if isinstance(cur, tuple):
                    v = tuple(float(x) for x in v)
                elif isinstance(cur, bool):
                    v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
                elif isinstance(cur, float):
                    v = float(v)
                setattr(s, f.name, v)
        return s

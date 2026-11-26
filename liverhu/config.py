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
    # RL2 must be at least this far (cranio-caudally) below RL1.
    min_rl_separation_mm: float = 30.0
    # When a DICOM study has several series, prefer slice thickness closest to this.
    preferred_slice_mm: float = 3.0
    # Also save the liver-segment mask (NIfTI) with each result.
    save_segmentation: bool = False

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

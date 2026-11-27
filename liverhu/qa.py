"""Quality-check figure: each ROI on its axial slice, a coronal overview and a summary.

Layout (2 x 3):  LL | RL1 | RL2
                 SPL | coronal overview | summary table
Images are shown in standard radiological convention: patient's RIGHT on the viewer's
LEFT, anterior at the top (axial) / superior at the top (coronal).
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Circle  # noqa: E402
import numpy as np  # noqa: E402

from .roi import ligament_points  # noqa: E402

WINDOW = (-160, 240)          # abdomen window W400 / L40
COLORS = {"LL": "#00e5ff", "RL1": "#ff4dd2", "RL2": "#ffb000", "SPL": "#4ade80"}
REGION_STYLE = [((2, 3), "#00e5ff", "Seg 2/3 (left lobe)"),
                ((4,), "#9cff57", "Seg 4"),
                ((5, 6, 7, 8), "#ff4dd2", "Seg 5-8 (right lobe)"),
                ((1,), "#b0b0b0", "Seg 1 (caudate)")]
SPLEEN_COLOR = "#4ade80"
PANEL = {"LL": (0, 0), "RL1": (0, 1), "RL2": (0, 2), "SPL": (1, 0)}


def _axial(arr2d):
    """canonical (x, y) slice -> display (rows = anterior->posterior, cols = pt right->left)."""
    return arr2d[::-1, ::-1].T


def _coronal(arr2d):
    """canonical (x, z) slice -> display (rows = superior->inferior, cols = pt right->left)."""
    return arr2d[::-1, ::-1].T


def _fmt(v, nd=1):
    return "-" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"{v:.{nd}f}"


def _orient(ax, top="A"):
    ax.text(0.01, 0.5, "R", color="white", transform=ax.transAxes, fontsize=12, va="center")
    ax.text(0.97, 0.5, "L", color="white", transform=ax.transAxes, fontsize=12, va="center")
    ax.text(0.5, 0.99, top, color="white", transform=ax.transAxes, fontsize=12, ha="center", va="top")
    ax.set_xticks([]); ax.set_yticks([])


def _axial_panel(ax, ct, r, spacing, contours, crop_mask, show_ligament_from=None, others=()):
    sx, sy, _ = spacing
    X, Y, _ = ct.shape
    ax.set_facecolor("black")
    if r is None or not r.placed:
        name = r.name if r is not None else "Spleen"
        reason = r.reason if r is not None else "not measured"
        ax.text(0.5, 0.5, f"{name}\nnot placed:\n{reason}", color="white", ha="center",
                va="center", transform=ax.transAxes, fontsize=10)
        ax.axis("off")
        return
    x, y, z = r.center_voxel
    cols_mm, rows_mm = np.arange(X) * sx, np.arange(Y) * sy
    ax.imshow(_axial(ct[:, :, z]), cmap="gray", vmin=WINDOW[0], vmax=WINDOW[1], interpolation="nearest",
              extent=(-0.5 * sx, (X - 0.5) * sx, (Y - 0.5) * sy, -0.5 * sy))
    for mask2d, color in contours(z):
        m = _axial(mask2d)
        if m.any():
            ax.contour(cols_mm, rows_mm, m.astype(float), levels=[0.5], colors=color, linewidths=0.8)
    if show_ligament_from is not None:
        pts = ligament_points(show_ligament_from[:, :, z])
        if len(pts):
            ax.scatter((X - 1 - pts[:, 0]) * sx, (Y - 1 - pts[:, 1]) * sy, s=7, c="yellow",
                       marker="s", linewidths=0, zorder=5)
    for o in others:                 # other circles on this same slice, dashed
        if o is not None and o.placed and o.key != r.key and o.center_voxel and o.center_voxel[2] == z:
            ox, oy = (X - 1 - o.center_voxel[0]) * sx, (Y - 1 - o.center_voxel[1]) * sy
            ax.add_patch(Circle((ox, oy), o.diameter_mm / 2, fill=False, ec=COLORS[o.key], lw=1.0, ls="--"))
            ax.text(ox, oy, o.key, color=COLORS[o.key], ha="center", va="center", fontsize=7, clip_on=True)
    cxm, cym = (X - 1 - x) * sx, (Y - 1 - y) * sy
    rad = r.diameter_mm / 2
    ax.add_patch(Circle((cxm, cym), rad, fill=False, ec=COLORS[r.key], lw=2.0))
    ax.text(cxm, cym - rad - 4, r.key, color=COLORS[r.key], ha="center", va="bottom", fontsize=11, weight="bold")
    region = np.argwhere(_axial(crop_mask[:, :, z]))
    if len(region):
        r0, c0 = region.min(axis=0)
        r1, c1 = region.max(axis=0)
        pad = 15.0 if r.key != "SPL" else 30.0
        ax.set_xlim(c0 * sx - pad, c1 * sx + pad)
        ax.set_ylim(r1 * sy + pad, r0 * sy - pad)
    _orient(ax)
    ax.set_title(f"{r.name}{' (moved by hand)' if getattr(r, 'manual', False) else ''} - axial z = {_fmt(r.z_mm)} mm"
                 + (f" (image {r.dicom_instance})" if getattr(r, "dicom_instance", None) else ""),
                 color="white", fontsize=10)
    where = ("spleen" if r.key == "SPL" else
             f"segment {r.segment_at_center}   {'LEFT' if r.side_of_ligament == 'left' else 'RIGHT'} of ligament"
             + ("" if r.side_ok else "  ⚠ CHECK SIDE"))
    warn = {"low": "  (!) LOW", "high": "  (!) HIGH"}.get(getattr(r, "hu_check", "") or "", "")
    txt = (f"mean {_fmt(r.mean_hu)}{warn}  SD {_fmt(r.sd_hu)}  min {_fmt(r.min_hu, 0)}  max {_fmt(r.max_hu, 0)} HU\n"
           f"area {_fmt(r.area_cm2, 2)} cm²  ({_fmt(r.diameter_mm, 0)} mm)   {where}")
    ax.text(0.5, -0.02, txt, color="white", transform=ax.transAxes, ha="center", va="top", fontsize=9)


def render_qa(ct, seg, rois, spacing, out_path: Path, title: str = "",
              spleen_mask=None, spleen_roi=None, summary: dict | None = None) -> None:
    sx, sy, sz = (float(s) for s in spacing)
    X, Y, Z = ct.shape
    fig, axes = plt.subplots(2, 3, figsize=(15, 10.5), facecolor="black")
    liver3 = seg > 0

    def liver_contours(z):
        return [(np.isin(seg[:, :, z], labels), color) for labels, color, _ in REGION_STYLE]

    everyone = list(rois) + [spleen_roi]
    for r in rois:
        _axial_panel(axes[PANEL[r.key]], ct, r, (sx, sy, sz), liver_contours, liver3, show_ligament_from=seg,
                     others=everyone)

    spl_mask = spleen_mask if spleen_mask is not None else np.zeros(ct.shape, bool)
    _axial_panel(axes[PANEL["SPL"]], ct, spleen_roi, (sx, sy, sz),
                 lambda z: [(spl_mask[:, :, z], SPLEEN_COLOR)], spl_mask, others=everyone)

    # coronal overview through the liver ROIs, with all ROI levels
    ax = axes[1, 1]
    ax.set_facecolor("black")
    all_rois = [r for r in list(rois) + [spleen_roi] if r is not None and r.placed]
    liver_placed = [r for r in rois if r.placed]
    j = (int(np.round(np.mean([r.center_voxel[1] for r in liver_placed]))) if liver_placed else
         (int(np.round(np.nonzero(liver3.any(axis=(0, 2)))[0].mean())) if liver3.any() else Y // 2))
    ax.imshow(_coronal(ct[:, j, :]), cmap="gray", vmin=WINDOW[0], vmax=WINDOW[1], interpolation="nearest",
              extent=(-0.5 * sx, (X - 0.5) * sx, (Z - 0.5) * sz, -0.5 * sz), aspect="equal")
    cols_mm, zrows = np.arange(X) * sx, np.arange(Z) * sz
    for labels, color, _ in REGION_STYLE:
        m = np.isin(_coronal(seg[:, j, :]), labels)
        if m.any():
            ax.contour(cols_mm, zrows, m.astype(float), levels=[0.5], colors=color, linewidths=0.8)
    sm = _coronal(spl_mask[:, j, :])
    if sm.any():
        ax.contour(cols_mm, zrows, sm.astype(float), levels=[0.5], colors=SPLEEN_COLOR, linewidths=0.8)
    for r in all_rois:
        x, _, z = r.center_voxel
        yrow = (Z - 1 - z) * sz
        ax.axhline(yrow, color=COLORS[r.key], lw=1.0, ls="--")
        ax.plot([(X - 1 - x) * sx], [yrow], "o", ms=7, mfc="none", mec=COLORS[r.key], mew=2)
        ax.text((X - 1 - x) * sx + 8, yrow - 3, r.key, color=COLORS[r.key], fontsize=10, weight="bold")
    organs = liver3 | spl_mask
    if organs.any():
        xs = np.nonzero(organs.any(axis=(1, 2)))[0]
        zs = np.nonzero(organs.any(axis=(0, 1)))[0]
        ax.set_xlim((X - 1 - xs.max()) * sx - 25, (X - 1 - xs.min()) * sx + 25)
        ax.set_ylim((Z - 1 - zs.min()) * sz + 25, (Z - 1 - zs.max()) * sz - 25)
    _orient(ax, top="S")
    ax.set_title("Coronal overview - ROI levels", color="white", fontsize=10)

    # summary table
    ax = axes[1, 2]
    ax.set_facecolor("black")
    ax.axis("off")
    lines = [f"{'ROI':<5}{'mean':>8}{'':<7}{'SD':>6}{'min':>6}{'max':>6}{'cm²':>7}"]
    for r in list(rois) + ([spleen_roi] if spleen_roi is not None else []):
        if r.placed:
            w = {"low": "(!)LOW", "high": "(!)HIGH"}.get(getattr(r, "hu_check", "") or "", "")
            lines.append(f"{r.key:<5}{_fmt(r.mean_hu):>8} {w:<6}{_fmt(r.sd_hu):>6}{_fmt(r.min_hu, 0):>6}"
                         f"{_fmt(r.max_hu, 0):>6}{_fmt(r.area_cm2, 2):>7}")
        else:
            lines.append(f"{r.key:<5}  not placed")
    s = summary or {}
    lines += ["", f"Liver mean (3 ROIs)  {_fmt(s.get('liver_mean_hu_3roi'))} HU",
              f"Spleen               {_fmt(s.get('spleen_mean_hu'))} HU",
              f"Liver - spleen       {_fmt(s.get('liver_minus_spleen_hu'))} HU",
              f"Liver / spleen       {_fmt(s.get('liver_spleen_ratio'), 2)}"]
    if s.get("quality"):
        lines += ["", f"Quality: {s['quality'].upper()}" + ("" if s.get("scan_complete", True) else "   SCAN INCOMPLETE"),
                  f"Contrast: {s.get('contrast_phase', '?')}"]
        for note in (s.get("completeness_notes") or []) + (s.get("quality_notes") or []):
            lines.append("  - " + (note if len(note) <= 44 else note[:43] + "…"))
    ax.text(0.02, 0.97, "\n".join(lines), color="white", family="monospace", fontsize=10,
            va="top", ha="left", transform=ax.transAxes)
    ax.set_title("Summary (HU)", color="white", fontsize=10)

    handles = [plt.Line2D([], [], color=c, lw=2, label=l) for _, c, l in REGION_STYLE]
    handles.append(plt.Line2D([], [], color=SPLEEN_COLOR, lw=2, label="Spleen"))
    handles.append(plt.Line2D([], [], color="yellow", marker="s", ls="none", ms=4, label="falciform ligament (est.)"))
    fig.legend(handles=handles, loc="lower center", ncol=6, fontsize=8, frameon=False, labelcolor="white")
    fig.suptitle(title, color="white", fontsize=12)
    fig.subplots_adjust(left=0.02, right=0.98, top=0.94, bottom=0.07, wspace=0.05, hspace=0.22)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=100, facecolor="black")
    plt.close(fig)
